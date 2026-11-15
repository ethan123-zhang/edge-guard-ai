"""采集：录板端 USB 声卡（`arecord` 子进程）与测试用假采集器。

板端只用 ALSA，不引入 sounddevice/portaudio：`arecord` 是 alsa-utils 自带的，
子进程出问题也只是这一路语音停，不会把整个 master 进程带崩。
"""
from __future__ import annotations

import re
import select
import subprocess
from typing import Callable, List, Optional


def turn_off_agc(device: str, log: Optional[Callable[[str], None]] = None) -> bool:
    """把 USB 麦的 AGC 关掉（`plughw:2,0` → card 2）。设备不支持就静默跳过。"""
    match = re.search(r"(\d+)", str(device or ""))
    if not match:
        return False
    card = match.group(1)
    try:
        subprocess.run(["amixer", "-c", card, "sset", "Auto Gain Control", "off"],
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                       timeout=5, check=False)
        return True
    except (OSError, subprocess.SubprocessError) as exc:
        if log:
            log("[voice] 关 AGC 失败（忽略）：%s" % exc)
        return False


class CaptureError(RuntimeError):
    """采集设备打不开（设备名错、被占用、没插麦克风）。"""


class ArecorderCapture(object):
    """`arecord -t raw` 子进程，按块读 PCM。"""

    def __init__(self, device: str, rate: int = 16000, channels: int = 1,
                 chunk_ms: int = 40, arecord: str = "arecord",
                 log: Optional[Callable[[str], None]] = None,
                 popen: Callable[..., subprocess.Popen] = subprocess.Popen):
        self.device = str(device)
        self.rate = int(rate)
        self.channels = int(channels)
        self.chunk_bytes = int(self.rate * self.channels * 2 * chunk_ms / 1000)
        self.chunk_bytes = max(2, self.chunk_bytes - (self.chunk_bytes % 2))
        self._arecord = arecord
        self._log = log or (lambda message: None)
        self._popen = popen
        self._proc: Optional[subprocess.Popen] = None

    def start(self) -> None:
        if self._proc is not None:
            return
        cmd = [self._arecord, "-q", "-D", self.device, "-f", "S16_LE",
               "-r", str(self.rate), "-c", str(self.channels), "-t", "raw"]
        try:
            self._proc = self._popen(cmd, stdout=subprocess.PIPE,
                                     stderr=subprocess.DEVNULL, bufsize=0)
        except OSError as exc:
            raise CaptureError("启动 arecord 失败：%s" % exc)

    def read(self) -> bytes:
        if self._proc is None or self._proc.stdout is None:
            raise CaptureError("采集器未启动")
        data = self._proc.stdout.read(self.chunk_bytes)
        if not data:
            raise CaptureError("采集流中断（arecord 退出）")
        return data

    def drain(self) -> None:
        """丢弃已缓冲的音频（播报期间/播报之后防自问自答）。"""
        if self._proc is None or self._proc.stdout is None:
            return
        stdin_fd = self._proc.stdout.fileno()
        while True:
            try:
                ready, _, _ = select.select([stdin_fd], [], [], 0)
            except (OSError, ValueError):
                return
            if not ready:
                return
            if not self._proc.stdout.read1(self.chunk_bytes):
                return

    def close(self) -> None:
        proc, self._proc = self._proc, None
        if proc is None:
            return
        try:
            if proc.poll() is None:
                proc.terminate()
                try:
                    proc.wait(timeout=1.0)
                except subprocess.TimeoutExpired:
                    proc.kill()
        except OSError:
            pass

    def is_alive(self) -> bool:
        return self._proc is not None and self._proc.poll() is None


class FakeCapture(object):
    """测试用：按预置块返回，读完后返回空块（模拟 EOF）。"""

    def __init__(self, chunks: List[bytes], chunk_size: int = 640):
        self._chunks = list(chunks)
        self.chunk_size = int(chunk_size)
        self._pending = b""
        self.drained = 0

    def start(self) -> None:
        return

    def read(self) -> bytes:
        if not self._pending:
            if not self._chunks:
                return b""
            self._pending = self._chunks.pop(0)
            if not self._pending:
                return b""
        data = self._pending[: self.chunk_size]
        self._pending = self._pending[self.chunk_size:]
        return data

    def drain(self) -> None:
        self.drained += 1

    def close(self) -> None:
        return

    def is_alive(self) -> bool:
        return True