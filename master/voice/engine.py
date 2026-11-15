"""语音闭环：采集 → VAD 切句 → 识别（云优先/离线回退）→ 接待入口 → 播报。

三条边界（和 2026-11-15 的决定对齐）：
1. **只在有活跃到访时识别**：没有 `visit_id` 时识别结果无处可去，直接丢弃，
   不产生空到访、不把声音写库。
2. **播报期间关麦**：播报前不读麦克风、播报后 `drain()` 丢掉残余、`VAD.reset()`，
   再延时开麦——否则喇叭声被录回来就是"自问自答"死循环（设计 §3.5 保命机制）。
3. **识别失败不打断主循环**：空文本只记日志/播「没听清」，异常在 `run()` 里兜底。

线程模型：`run(stop_event)` 在 `master-voice` 线程里跑；访问数据库一律走
`reception` 并持有调用方传进来的锁（与 HTTP/视觉线程共用 `app.lock`）。
"""
from __future__ import annotations

import threading
import time
from typing import Any, Callable, Dict, Optional

from master.voice.asr import (AsrChain, clip_ratio, complete_prefix, match_phrase,
                              normalize_text)


class VoiceEngine(object):
    def __init__(self, cfg: Dict[str, Any], reception, current_visit: Callable[[], Optional[str]],
                 capture, asr: AsrChain, player=None, phrasebook=None,
                 lock: Optional[threading.RLock] = None,
                 broadcast: Optional[Callable[[], None]] = None,
                 log: Optional[Callable[[str], None]] = None,
                 sleep: Callable[[float], None] = time.sleep,
                 now_ms: Optional[Callable[[], int]] = None):
        self.cfg = cfg or {}
        self.reception = reception
        self._current_visit = current_visit
        self.capture = capture
        self.asr = asr
        self.player = player
        self.phrasebook = phrasebook
        self._lock = lock or threading.RLock()
        self._broadcast = broadcast
        self._log = log or (lambda message: None)
        self._sleep = sleep
        self._now_ms = now_ms or (lambda: int(time.time() * 1000))

        from master.voice.vad import VadSegmenter

        vad_cfg = self.cfg.get("vad") or {}
        self.vad = VadSegmenter(
            rate=int((self.cfg.get("capture") or {}).get("rate", 16000)),
            speech_db=float(vad_cfg.get("speech_db", 8.0)),
            hangover_ms=int(vad_cfg.get("hangover_ms", 600)),
            min_speech_ms=int(vad_cfg.get("min_speech_ms", 300)),
            max_utterance_ms=int(vad_cfg.get("max_utterance_ms", 8000)),
            pre_roll_ms=int(vad_cfg.get("pre_roll_ms", 200)),
        )
        playback_cfg = self.cfg.get("playback") or {}
        self.post_delay_ms = int(playback_cfg.get("post_delay_ms", 300))
        self._not_heard_file = str(playback_cfg.get("not_heard") or "")
        asr_cfg = self.cfg.get("asr") or {}
        offline_cfg = asr_cfg.get("offline") or {}
        self._grammar_phrases = list(offline_cfg.get("grammar") or [])
        self._max_clip_ratio = float(asr_cfg.get("max_clip_ratio", 0.01))
        self._noise_text_chars = int(asr_cfg.get("noise_text_chars", 3))
        self._speaking = False
        self._play_lock = threading.Lock()
        self._running = False
        self._last_error: Optional[str] = None
        self._last_asr: Dict[str, Any] = {}
        self._utterances = 0
        self._dropped = 0
        self._dropped_noise = 0

    # ---------------------------------------------------------------- 播报

    def _not_heard_path(self) -> Optional[str]:
        if not self._not_heard_file or self.phrasebook is None:
            return None
        root = getattr(self.phrasebook, "root", None)
        if root is None:
            return None
        path = root / self._not_heard_file
        return str(path) if path.is_file() else None

    def speak_text(self, text: str) -> bool:
        """按话术映射播一条回复（视觉线程也会调：开场问候要出声）。"""
        if self.player is None or self.phrasebook is None:
            return False
        wav = self.phrasebook.wav_for(text)
        if not wav:
            return False
        return self._play_file(wav)

    def speak_cue(self, cue: str) -> bool:
        """按话术 cue 播固定 wav（动态内容只上看板）。"""
        if self.player is None or self.phrasebook is None or not cue:
            return False
        wav = self.phrasebook.wav_for_cue(cue)
        if not wav:
            return False
        return self._play_file(wav)

    def _play_file(self, path: str) -> bool:
        """播一个 wav：整段（播放 + 尾延时）都按住 `_speaking`，播完清采集缓冲。"""
        with self._play_lock:
            self._speaking = True
            ok = False
            try:
                ok = bool(self.player.play(path)) if self.player is not None else False
                if self.post_delay_ms:
                    self._sleep(self.post_delay_ms / 1000.0)
            finally:
                self._safe_drain()
                self._speaking = False
            return ok

    def _safe_drain(self) -> None:
        try:
            self.capture.drain()
        except Exception:
            pass
        self.vad.reset()

    # ---------------------------------------------------------------- 主循环

    def step(self) -> Optional[Dict[str, Any]]:
        """读一块音频并处理；返回本次处理摘要，没处理就返回 None。"""
        chunk = self.capture.read()
        if not chunk:
            return None
        if self._speaking:  # 播报中不收音
            return None
        handled = []
        for event in self.vad.feed(chunk):
            if event.get("type") != "utterance":
                continue
            handled.append(self._handle_utterance(event["pcm"], event.get("duration_ms")))

        return handled or None

    def _handle_utterance(self, pcm: bytes, duration_ms: Optional[int] = None) -> Dict[str, Any]:
        visit_id = self._current_visit()
        if not visit_id:
            self._dropped += 1
            return {"visit_id": None, "dropped": "no_visit", "duration_ms": duration_ms}

        ratio = clip_ratio(pcm)
        if ratio > self._max_clip_ratio:
            self._dropped_noise += 1
            self._log("[voice] 丢弃削顶段（满幅样本 %.1f%%）" % (ratio * 100.0))
            return {"visit_id": visit_id, "dropped": "clipped",
                    "clip_ratio": round(ratio, 3)}

        result = self.asr.transcribe(pcm)
        text = str(result.get("text") or "").strip()
        if self._grammar_phrases:
            completed = complete_prefix(text, self._grammar_phrases)
            if completed and completed != normalize_text(text):
                self._log("[voice] 识别补全：%r → %r" % (text, completed))
                text = completed
        if (text and self._grammar_phrases
                and len(normalize_text(text)) <= self._noise_text_chars
                and match_phrase(text, self._grammar_phrases) is None):
            self._dropped_noise += 1
            self._last_asr = {"text": text, "path": result.get("path"),
                              "error": "noise_text", "duration_ms": duration_ms}
            self._log("[voice] 丢弃疑似噪声文本：%r" % text)
            return {"visit_id": visit_id, "dropped": "noise_text", "text": text}
        self._last_asr = {"text": text, "path": result.get("path"),
                          "error": result.get("error"),
                          "duration_ms": duration_ms}
        if not text:
            not_heard = self._not_heard_path()
            if not_heard:
                self._play_file(not_heard)
            return {"visit_id": visit_id, "text": "", "reply": None,
                    "path": result.get("path"), "error": result.get("error")}

        utterance_id = "voice:%d:%d" % (self._now_ms(), self._utterances + 1)
        with self._lock:
            outcome = self.reception.on_utterance(visit_id, text, utterance_id,
                                                  source="voice")
        self._utterances += 1
        if self._broadcast is not None:
            try:
                self._broadcast()
            except Exception:
                pass
        reply = outcome.get("reply")
        cue = outcome.get("cue")
        played = False
        if cue:
            played = self.speak_cue(cue)
        if not played and reply:
            played = self.speak_text(reply)
        return {"visit_id": visit_id, "text": text, "reply": reply, "cue": cue,
                "played": played,
                "path": result.get("path"), "alert": bool(outcome.get("alert")),
                "closed": bool(outcome.get("closed"))}

    def run(self, stop_event: threading.Event) -> None:
        self._running = True
        try:
            self.capture.start()
        except Exception as exc:
            self._last_error = str(exc)
            self._running = False
            self._log("[voice] 采集启动失败，语音线停用：%s" % exc)
            return
        self._log("[voice] 语音线已启动（%s）" % self.cfg.get("capture", {}).get("device"))
        try:
            while not stop_event.is_set():
                try:
                    self.step()
                except Exception as exc:  # 采集流中断等：记下来继续，不杀进程
                    self._last_error = str(exc)
                    self._log("[voice] 单步异常：%s" % exc)
                    if not getattr(self.capture, "is_alive", lambda: True)():
                        break
                    time.sleep(0.2)
        finally:
            self._running = False
            try:
                self.capture.close()
            except Exception:
                pass
            self._log("[voice] 语音线已停止")

    # ---------------------------------------------------------------- 状态

    def status(self) -> Dict[str, Any]:
        return {"enabled": True, "running": self._running,
                "speaking": self._speaking, "utterances": self._utterances,
                "dropped_no_visit": self._dropped,
                "dropped_noise": self._dropped_noise,
                "last_asr": self._last_asr,
                "last_error": self._last_error}