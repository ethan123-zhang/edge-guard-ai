"""能量 VAD：只用标准库，不引入第三方依赖。

判据是"短时 RMS 超过自适应底噪 + N dB"，底噪取最近窗口的 20 分位，
这样空调声、风扇声抬高时不会把底噪一起带歪。端点由 hangover 决定。

注意这是**端点检测**不是识别：它只负责切出一句话交给 ASR，
所以宁可切早（带 200 ms pre-roll）也不要吃掉字头。
"""
from __future__ import annotations

import array
import math
from collections import deque
from typing import Any, Deque, Dict, List


def level_db(frame: bytes) -> float:
    """S16_LE 单声道一帧的 dBFS（-96 表示静音）。"""
    if not frame:
        return -96.0
    samples = array.array("h")
    samples.frombytes(frame[: len(frame) - (len(frame) % 2)])
    if not samples:
        return -96.0
    total = 0
    for value in samples:
        total += value * value
    rms = math.sqrt(total / float(len(samples)))
    if rms <= 1.0:
        return -96.0
    return 20.0 * math.log10(rms / 32768.0)


class VadSegmenter(object):
    """把连续 PCM 切成一句话一段。

    `feed()` 返回事件列表，事件形如
    `{"type": "utterance", "pcm": b"...", "duration_ms": 1200}`。
    """

    def __init__(self, rate: int = 16000, frame_ms: int = 40,
                 speech_db: float = 8.0, hangover_ms: int = 600,
                 min_speech_ms: int = 300, max_utterance_ms: int = 8000,
                 pre_roll_ms: int = 200, noise_window: int = 100,
                 noise_percentile: int = 20):
        self.rate = int(rate)
        self.frame_ms = int(frame_ms)
        self.frame_bytes = int(self.rate * self.frame_ms / 1000) * 2
        self.speech_db = float(speech_db)
        self.hangover_frames = max(1, int(hangover_ms / max(self.frame_ms, 1)))
        self.min_speech_frames = max(1, int(math.ceil(min_speech_ms / float(self.frame_ms))))
        self.max_frames = max(self.min_speech_frames, int(max_utterance_ms / max(self.frame_ms, 1)))
        self.pre_frames = max(0, int(pre_roll_ms / max(self.frame_ms, 1)))
        self.noise_window = max(5, int(noise_window))
        self.noise_percentile = max(1, min(99, int(noise_percentile)))
        self._buf = b""
        self._levels: Deque[float] = deque(maxlen=self.noise_window)
        self._pre: Deque[bytes] = deque(maxlen=max(1, self.pre_frames))
        self._frames: List[bytes] = []
        self._silence = 0
        self._speech = 0
        # 前 5 帧（200 ms）只用来量底噪：房间开空调/风扇时不会一开机就当成说话
        self._warmup = 5

    # ---------------------------------------------------------------- 底噪

    def noise_db(self) -> float:
        if not self._levels:
            return -60.0
        ordered = sorted(self._levels)
        index = int((len(ordered) - 1) * self.noise_percentile / 100.0)
        return ordered[index]

    def reset(self) -> None:
        self._buf = b""
        self._pre.clear()
        self._frames = []
        self._silence = 0
        self._speech = 0

    # ---------------------------------------------------------------- 主循环

    def feed(self, pcm: bytes) -> List[Dict[str, Any]]:
        if pcm:
            self._buf += pcm
        events: List[Dict[str, Any]] = []
        while len(self._buf) >= self.frame_bytes:
            frame = self._buf[: self.frame_bytes]
            self._buf = self._buf[self.frame_bytes:]
            event = self._push_frame(frame)
            if event is not None:
                events.append(event)
        return events

    def _push_frame(self, frame: bytes) -> Dict[str, Any] | None:
        level = level_db(frame)
        if not self._frames and len(self._levels) < self._warmup:
            self._levels.append(level)
            self._pre.append(frame)
            return None
        active = level > self.noise_db() + self.speech_db
        if active:
            if not self._frames:
                self._frames.extend(self._pre)
                self._pre.clear()
            self._frames.append(frame)
            self._speech += 1
            self._silence = 0
        else:
            self._levels.append(level)
            if self._frames:
                self._frames.append(frame)
                self._silence += 1
                if self._silence >= self.hangover_frames:
                    return self._finish()
            else:
                self._pre.append(frame)
        if self._frames and len(self._frames) >= self.max_frames:
            return self._finish(force=True)
        return None

    def _finish(self, force: bool = False) -> Dict[str, Any] | None:
        frames = self._frames
        speech_frames = self._speech
        # 去掉尾部的 hangover 静音（但至少留一帧）
        trim = 0 if force else min(self._silence, max(0, len(frames) - 1))
        body = frames[: len(frames) - trim] if trim else list(frames)
        self._frames = []
        self._silence = 0
        self._speech = 0
        if speech_frames < self.min_speech_frames:
            return None
        return {"type": "utterance", "pcm": b"".join(body),
                "duration_ms": len(body) * self.frame_ms}