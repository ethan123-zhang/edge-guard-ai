"""语音识别：云优先 + 离线兜底（用户 2026-11-15 定）。

规则（同时写进 docs/03 与配置注释）：
1. 云 ASR 可用时先用云；**超时/报错/空结果**自动落到离线 Vosk，并记下本次用的哪条路；
2. 云没配 key 或网络断（现场断网演示段）时，只有离线一条路；
3. 两条都不可用就返回空文本，由引擎决定播「没听清」——**不抛异常到主循环**。

离线识别用**受限词表**（Vosk grammar）：演示只需要听懂十来句固定话，
闭集识别能把同音错误压下去（实测近讲自由识别 3/4，加词表 4/4）。
"""
from __future__ import annotations

import base64
import json
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Callable, Dict, Iterable, List, Optional

BAIDU_TOKEN_URL = "https://aip.baidubce.com/oauth/2.0/token"
BAIDU_ASR_URL = "https://vop.baidu.com/server_api"


def normalize_text(text: str) -> str:
    """去掉 Vosk 输出的词间空格（中文词表匹配靠子串，带空格会匹配不上）。"""
    return "".join(str(text or "").split())


_PUNCT = "，。！？；：、,.!?;:…—-\"'“”‘’（）()"

def _clean(text: str) -> str:
    return "".join(ch for ch in normalize_text(text) if ch not in _PUNCT)


def complete_prefix(text: str, phrases: Optional[Iterable[str]],
                    min_chars: int = 2, max_missing: int = 2) -> str:
    """识别文本是词表短语的**前段**时补全（防 VAD 把句尾切掉）。

    例：词表有「把门打开」，识别成「把门打」→ 补成「把门打开」。
    只在"差 ≤ max_missing 个字"时补，且原文至少 min_chars 字，避免过度脑补。
    """
    clean = _clean(text)
    if not clean or not phrases or len(clean) < min_chars:
        return clean
    for phrase in phrases:
        target = _clean(phrase)
        if not target or not target.startswith(clean):
            continue
        missing = len(target) - len(clean)
        if 0 < missing <= max_missing:
            return target
    return clean


def match_phrase(text: str, phrases: Optional[Iterable[str]]) -> Optional[str]:
    """文本是否落在词表短语里（相等/包含）；落在返回该短语，否则 None。"""
    clean = _clean(text)
    if not clean or not phrases:
        return None
    for phrase in phrases:
        target = _clean(phrase)
        if not target:
            continue
        if target == clean or target in clean:
            return target
    return None


def clip_ratio(pcm: bytes) -> float:
    """削顶样本占比：接近满幅的段多半是敲麦/插拔噪声，不是人声。"""
    if not pcm:
        return 0.0
    import array

    samples = array.array("h")
    samples.frombytes(pcm[: len(pcm) - (len(pcm) % 2)])
    if not samples:
        return 0.0
    clipped = sum(1 for value in samples if abs(value) >= 32000)
    return clipped / float(len(samples))


class CloudAsrError(RuntimeError):
    """云识别不可用（网络/鉴权/服务端错误）。"""


class BaiduAsr(object):
    """百度短语音识别（`vop.baidu.com`）。

    需要 API Key / Secret Key（百度智能云控制台建应用）；`transport` 可注入，
    单测里用假 transport 验证请求形状，**不需要真实网络**。
    """

    def __init__(self, api_key: str, secret_key: str, timeout_s: float = 3.0,
                 rate: int = 16000, cuid: str = "sentinel-lubancat",
                 transport: Optional[Callable[..., Any]] = None):
        self.api_key = str(api_key or "").strip()
        self.secret_key = str(secret_key or "").strip()
        self.timeout_s = float(timeout_s)
        self.rate = int(rate)
        self.cuid = str(cuid)
        self._transport = transport or self._http
        self._token = ""
        self._token_expires_at = 0.0

    # ---------------------------------------------------------------- 传输

    def _http(self, method: str, url: str, data: Optional[bytes],
              headers: Dict[str, str], timeout_s: float) -> bytes:
        request = urllib.request.Request(url, data=data, headers=headers, method=method)
        with urllib.request.urlopen(request, timeout=timeout_s) as response:
            return response.read()

    def _token_value(self) -> str:
        now = time.time()
        if self._token and now < self._token_expires_at:
            return self._token
        query = urllib.parse.urlencode({
            "grant_type": "client_credentials",
            "client_id": self.api_key,
            "client_secret": self.secret_key,
        })
        try:
            raw = self._transport("GET", BAIDU_TOKEN_URL + "?" + query, None,
                                  {"Accept": "application/json"}, self.timeout_s)
            payload = json.loads(raw.decode("utf-8", "replace"))
        except (urllib.error.URLError, OSError, ValueError) as exc:
            raise CloudAsrError("取百度 token 失败：%s" % exc)
        token = str(payload.get("access_token") or "")
        if not token:
            raise CloudAsrError("百度未返回 access_token：%s" % (payload.get("error_description") or payload))
        expires_in = int(payload.get("expires_in") or 0)
        self._token = token
        self._token_expires_at = now + max(60, expires_in - 60)
        return token

    # ---------------------------------------------------------------- 识别

    def transcribe(self, pcm: bytes) -> Dict[str, Any]:
        if not pcm:
            return {"text": "", "path": "cloud", "error": "empty_audio"}
        token = self._token_value()
        body = json.dumps({
            "format": "pcm",
            "rate": self.rate,
            "channel": 1,
            "cuid": self.cuid,
            "token": token,
            "speech": base64.b64encode(pcm).decode("ascii"),
            "len": len(pcm),
        }).encode("utf-8")
        try:
            raw = self._transport("POST", BAIDU_ASR_URL, body,
                                  {"Content-Type": "application/json",
                                   "Accept": "application/json"}, self.timeout_s)
            payload = json.loads(raw.decode("utf-8", "replace"))
        except (urllib.error.URLError, OSError, ValueError) as exc:
            raise CloudAsrError("百度识别请求失败：%s" % exc)
        err_no = int(payload.get("err_no") or 0)
        if err_no != 0:
            raise CloudAsrError("百度识别返回 err_no=%s %s" % (err_no, payload.get("err_msg")))
        results = payload.get("result") or []
        text = normalize_text("".join(str(item) for item in results))
        return {"text": text, "path": "cloud", "error": None}


class VoskAsr(object):
    """离线 Vosk（板端 `master` 的 venv 里装，模型放本机目录）。"""

    def __init__(self, model_dir: str, grammar: Optional[Iterable[str]] = None,
                 rate: int = 16000, log: Optional[Callable[[str], None]] = None):
        self.model_dir = str(model_dir)
        self.rate = int(rate)
        self.grammar = [str(item) for item in grammar] if grammar else None
        self._log = log or (lambda message: None)
        try:
            from vosk import Model, SetLogLevel  # 板端依赖，PC 上没有时由 create_engine 捕获
        except ImportError as exc:  # pragma: no cover - 板端才装
            raise CloudAsrError("未安装 vosk：%s" % exc)
        SetLogLevel(-1)   # 模型加载的 Kaldi LOG 很吵，别把 journal 刷满
        self._model = Model(self.model_dir)
        if self.grammar:
            self._log("[voice] 离线识别启用受限词表（%d 条）" % len(self.grammar))

    def transcribe(self, pcm: bytes) -> Dict[str, Any]:
        from vosk import KaldiRecognizer, SetLogLevel

        SetLogLevel(-1)
        if self.grammar:
            recognizer = KaldiRecognizer(self._model, self.rate,
                                         json.dumps(self.grammar, ensure_ascii=False))
        else:
            recognizer = KaldiRecognizer(self._model, self.rate)
        recognizer.AcceptWaveform(pcm)
        payload = json.loads(recognizer.FinalResult())
        text = normalize_text(payload.get("text") or "")
        return {"text": text, "path": "offline", "error": None}


class AsrChain(object):
    """按模式选路：`cloud_first` / `cloud` / `offline`。"""

    def __init__(self, cloud=None, offline=None, mode: str = "cloud_first",
                 log: Optional[Callable[[str], None]] = None):
        self.cloud = cloud
        self.offline = offline
        self.mode = str(mode or "cloud_first")
        self._log = log or (lambda message: None)
        self.last_error: Optional[str] = None

    def transcribe(self, pcm: bytes) -> Dict[str, Any]:
        errors: List[str] = []
        if self.mode in ("cloud", "cloud_first") and self.cloud is not None:
            try:
                result = self.cloud.transcribe(pcm)
                if result.get("text"):
                    self.last_error = None
                    return result
                errors.append("cloud:empty")
            except Exception as exc:  # 云侧任何异常都不许把主循环打断
                errors.append("cloud:%s" % exc)
                self._log("[voice] 云识别失败，按规则回退：%s" % exc)
        if self.mode in ("offline", "cloud_first") and self.offline is not None:
            try:
                result = self.offline.transcribe(pcm)
                if errors:
                    result["error"] = ";".join(errors)
                self.last_error = ";".join(errors) or None
                return result
            except Exception as exc:
                errors.append("offline:%s" % exc)
        self.last_error = ";".join(errors) or "no_asr"
        return {"text": "", "path": "none", "error": self.last_error}