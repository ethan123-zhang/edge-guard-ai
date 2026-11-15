"""播报出口：`aplay` 播固定话术 wav。

第 4 阶段口径（用户 2026-11-11 定）：播报**只播预置话术**，动态内容
（姓名 / 第几次 / 原话）走看板字幕。所以这里不做 TTS 合成，只做
"话术 cue → wav 文件" 的映射；映射不到就静音，交给看板文字兜底。

cue 比"按回复原文匹配"稳：回复里可能带访客姓名/次数（动态），
但音频永远是同一条固定话术。
"""
from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Callable, Dict, Optional


class WavPlayer(object):
    """用 `aplay` 播一段 wav；失败只记日志，不影响接待主流程。"""

    def __init__(self, device: str, aplay: str = "aplay",
                 run: Callable[..., subprocess.CompletedProcess] = subprocess.run,
                 log: Optional[Callable[[str], None]] = None):
        self.device = str(device)
        self._aplay = aplay
        self._run = run
        self._log = log or (lambda message: None)

    def play(self, path: str) -> bool:
        try:
            result = self._run([self._aplay, "-q", "-D", self.device, str(path)],
                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                               timeout=30)
            return getattr(result, "returncode", 0) == 0
        except (OSError, subprocess.SubprocessError) as exc:
            self._log("[voice] 播报失败：%s" % exc)
            return False


class NullPlayer(object):
    """没有播报出口时的占位（测试 / 没接音箱）。"""

    def play(self, path: str) -> bool:
        return False


class PhraseBook(object):
    """话术 cue / 回复原文 → 预置 wav 文件。

    配置形如（config/master.yaml 的 voice.playback）：
        cues:
          greeting_unknown: greeting_unknown.wav
          purpose_ack: purpose_ack.wav
    文件放 `dir` 下；映射不到、文件缺、目录没配都返回 None（静音）。
    """

    def __init__(self, mapping: Optional[Dict[str, str]] = None,
                 cues: Optional[Dict[str, str]] = None,
                 root: Optional[str] = None,
                 log: Optional[Callable[[str], None]] = None):
        self.mapping = {str(key): str(value) for key, value in (mapping or {}).items()}
        self.cues = {str(key): str(value) for key, value in (cues or {}).items()}
        self.root = Path(root) if root else None
        self._log = log or (lambda message: None)
        self._missing_logged = set()

    def wav_for_cue(self, cue: str) -> Optional[str]:
        return self._resolve(self.cues.get(str(cue or "").strip()), str(cue))

    def wav_for(self, reply: str) -> Optional[str]:
        return self._resolve(self.mapping.get(str(reply or "").strip()), reply)

    def _resolve(self, name: Optional[str], key: str) -> Optional[str]:
        if not name:
            return None
        if self.root is None:
            self._log_once(name, "[voice] 播报映射到 %s，但未配置 playback.dir" % name)
            return None
        path = self.root / name
        if not path.is_file():
            self._log_once(name, "[voice] 话术文件不存在：%s" % path)
            return None
        return str(path)

    def _log_once(self, key: str, message: str) -> None:
        if key in self._missing_logged:
            return
        self._missing_logged.add(key)
        self._log(message)