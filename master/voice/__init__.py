"""语音线装配：配置 → 采集 / 识别链 / 播报，产出 `VoiceEngine`。

装配失败（没装 vosk、模型目录不对、设备名错）只记日志并返回 `None`，
master 进程照常起——语音是加分项，不许拖着整机服务一起挂。
"""
from __future__ import annotations

import os
from typing import Any, Callable, Optional

from master import config as master_config
from master.voice.asr import AsrChain, BaiduAsr, VoskAsr
from master.voice.capture import ArecorderCapture, turn_off_agc
from master.voice.engine import VoiceEngine
from master.voice.playback import NullPlayer, PhraseBook, WavPlayer


def _cloud_keys(cloud_cfg: dict):
    """密钥只从环境变量或本地覆盖文件读（与 security 的写法一致）。"""
    api_key = os.environ.get("SENTINEL_BAIDU_API_KEY") or str(cloud_cfg.get("api_key") or "")
    secret = os.environ.get("SENTINEL_BAIDU_SECRET_KEY") or str(cloud_cfg.get("secret_key") or "")
    return api_key.strip(), secret.strip()


def create_engine(app, log: Optional[Callable[[str], None]] = None) -> Optional[VoiceEngine]:
    log = log or (lambda message: None)
    cfg = (app.cfg or {}).get("voice") or {}
    if not cfg.get("enabled"):
        return None

    capture_cfg = cfg.get("capture") or {}
    device = str(capture_cfg.get("device") or "").strip()
    if not device:
        log("[voice] capture.device 为空，语音线不启动")
        return None
    capture = ArecorderCapture(
        device=device,
        rate=int(capture_cfg.get("rate", 16000)),
        channels=int(capture_cfg.get("channels", 1)),
        chunk_ms=int(capture_cfg.get("chunk_ms", 40)),
        log=log,
    )
    if capture_cfg.get("agc_off"):
        if turn_off_agc(device, log=log):
            log("[voice] 已关闭麦克风 AGC（固定增益）")

    asr_cfg = cfg.get("asr") or {}
    mode = str(asr_cfg.get("mode") or "cloud_first")
    offline = None
    offline_cfg = asr_cfg.get("offline") or {}
    model_dir = str(offline_cfg.get("model_dir") or "").strip()
    if mode in ("offline", "cloud_first") and model_dir:
        grammar = offline_cfg.get("grammar") or []
        try:
            offline = VoskAsr(model_dir, grammar=grammar, log=log)
            log("[voice] 离线识别就绪：%s" % model_dir)
        except Exception as exc:
            log("[voice] 离线识别不可用：%s" % exc)

    cloud = None
    cloud_cfg = asr_cfg.get("cloud") or {}
    api_key, secret = _cloud_keys(cloud_cfg)
    if mode in ("cloud", "cloud_first"):
        if api_key and secret:
            cloud = BaiduAsr(api_key, secret,
                             timeout_s=float(cloud_cfg.get("timeout_s", 3.0)),
                             rate=int(capture_cfg.get("rate", 16000)),
                             cuid=str(cloud_cfg.get("cuid") or "sentinel-lubancat"))
            log("[voice] 云识别就绪（baidu，超时 %.1fs）" % float(cloud_cfg.get("timeout_s", 3.0)))
        else:
            log("[voice] 云识别未配置 key，按规则走离线")

    chain = AsrChain(cloud=cloud, offline=offline, mode=mode, log=log)
    if cloud is None and offline is None:
        log("[voice] 云和离线都不可用，语音线不启动")
        return None

    playback_cfg = cfg.get("playback") or {}
    phrasebook = PhraseBook(
        mapping=playback_cfg.get("map") or {},
        cues=playback_cfg.get("cues") or {},
        root=str(master_config.resolve_project(playback_cfg["dir"])) if playback_cfg.get("dir") else None,
        log=log,
    )
    if playback_cfg.get("enabled") and playback_cfg.get("device"):
        player = WavPlayer(str(playback_cfg["device"]), log=log)
    else:
        player = NullPlayer()

    return VoiceEngine(
        cfg=cfg,
        reception=app.reception,
        current_visit=app.voice_current_visit,
        capture=capture,
        asr=chain,
        player=player,
        phrasebook=phrasebook,
        lock=app.lock,
        broadcast=app.broadcast_state,
        log=log,
    )