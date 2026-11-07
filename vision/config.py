"""配置加载：`base.yaml` + profile 覆盖（docs/02 §7.1；铁律 ④ 阈值全部外置）。

为什么自己写一层而不是到处 `yaml.safe_load`：
- **合并**：base 里有全部默认值，`sim` / `board` 只覆盖差异项，现场改一个数就够
- **校验**：配置写错要在启动那一秒报"哪个键不对"，而不是跑到一半 KeyError
- **3.6 兼容**：板端 Python 3.6，本模块不用 dataclasses / 未来注解

典型用法：
    from vision import config
    cfg = config.load("sim")
    cfg["detection"]["score_threshold"]      # 0.6
    config.resolve_vision(cfg["detection"]["model"])   # -> vision/models/xxx.onnx
"""
import copy
import os
from pathlib import Path

try:
    import yaml
except ImportError:  # pragma: no cover - 板端缺 PyYAML 时要给出可执行的修复命令
    yaml = None

VISION_DIR = Path(__file__).resolve().parent
PROJECT_DIR = VISION_DIR.parent
CONFIG_DIR = VISION_DIR / "config"


class ConfigError(ValueError):
    """配置非法。消息里必须带出**是哪个键**，现场改配置全靠它。"""


# 全部默认值都放在这里：base.yaml 只写"值得看见"的项，
# 其余靠默认值兜住，避免 base.yaml 漏一项就崩。
DEFAULTS = {
    "profile": "sim",
    "camera": {
        "source": 0,          # int=摄像头序号；str=视频文件或图片目录
        "backend": "auto",    # auto|dshow|msmf|v4l2|any
        "width": 1280,
        "height": 720,
        "mirror": False,      # 预览镜像（不影响送模型的图）
        "warmup_frames": 3,   # 摄像头刚开时常出黑帧/自动曝光跳变
    },
    "roi": {
        "enabled": False,
        "x": 0.0, "y": 0.0, "w": 1.0, "h": 1.0,   # 归一化 0~1
    },
    "detection": {
        "backend": "auto",    # auto|yunet|haar
        "model": "models/face_detection_yunet_2023mar.onnx",
        "score_threshold": 0.6,
        "nms_threshold": 0.3,
        "top_k": 50,
        "min_face_px": 80,    # 小于此像素的人脸忽略（远处观众不该触发）
        "haar_cascade": "",   # 空=用 OpenCV 自带的 frontalface_default
        "haar_min_neighbors": 5,
        "haar_equalize": True,  # Haar 是否做直方图均衡化；jetson_local 显式关
    },
    "recognition": {
        "backend": "auto",    # auto|sface|none
        "model": "models/face_recognition_sface_2021dec.onnx",
        "match_threshold": 0.363,   # SFace 官方建议的余弦阈值
        "gallery_dir": "face_db",
        "confirm_frames": 3,        # 连续 N 帧确认才算一个人
        "cooldown_sec": 10.0,       # 同一人重复上报冷却（docs/02 §3.1）
        "track_ttl_sec": 2.0,       # 目标丢失多久后丢轨道
    },
    "association": {
        "enabled": False,        # 短期匿名关联层；默认关闭，打开前先过场景素材验收
        "tau": 0.45,             # 本方相似度阈值（LFW 500 异人对 FAR=0）
        "margin": 0.10,          # 本方 - 异方 的最小边距
        "quality_max_rms": 0.10, # 入册质量门：对齐残差 ÷ 眼距
        "session_ttl_sec": 120.0,
        "track_ttl_sec": 2.0,
        "max_sessions": 64,
        "max_refs": 8,
    },
    "server": {
        "enabled": True,
        "ws_url": "ws://127.0.0.1:8765",
        "heartbeat_sec": 5.0,
        "reconnect_sec": 3.0,
    },
    "output": {
        "snapshot_dir": "snapshots",   # 相对仓库根，已 gitignore
        "save_snapshots": True,
        "snapshot_quality": 90,
        "print_events": True,
    },
    "runtime": {
        "max_fps": 0,          # 0=不限；PC 仿真时压到 15 更省 CPU
        "duration_sec": 0,     # 0=跑到 Ctrl-C
        "headless": False,
        "log_level": "info",
        # 检测/识别后端（2026-11-07 P1）。板上没有 cv2.FaceDetectorYN，
        # 只有 cpu/auto/trt 这三条「裸 ONNX」路径能用 YuNet + SFace：
        #   legacy = 旧路径（FaceDetectorYN / Haar + FaceRecognizerSF），默认不变
        #   auto   = 检测/识别各自判断：engine 可用走 TRT，否则退 CPU dnn
        #   cpu    = 强制 CPU dnn（不碰 TensorRT）
        #   trt    = 强制 TensorRT，engine 不可用直接报错，绝不静默降级
        "backend": "legacy",
        # 320 声明的 YuNet 实验副本：板上 OpenCV 4.1.1 也能导入（2023 版不能）
        "detector_onnx": "models/yunet_2022_opset9_experiment.onnx",
        "detector_engine": "",      # 空=没有 engine；相对路径按 vision/ 解析
        "recognizer_engine": "",
        "recognizer_batch": 8,      # SFace engine 的 profile 上限就是 8
        "warmup": True,             # 启动时跑一张空白图，别把首帧抖动留给现场
    },
}

_BACKEND_CHOICES = {
    "detection": ("auto", "yunet", "haar"),
    "recognition": ("auto", "sface", "none"),
}

_RUNTIME_BACKENDS = ("legacy", "auto", "cpu", "trt")

_CAMERA_BACKENDS = ("auto", "dshow", "msmf", "v4l2", "any")


def _deep_merge(base, override):
    """递归合并：override 覆盖 base，dict 逐层合并，其它类型整体替换。"""
    out = copy.deepcopy(base)
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = _deep_merge(out[key], value)
        else:
            out[key] = copy.deepcopy(value)
    return out


def _read_yaml(path):
    if yaml is None:
        raise ConfigError(
            "缺少 PyYAML：Ubuntu 上执行 `sudo apt-get install -y python3-yaml`，"
            "Windows 上执行 `python -m pip install pyyaml`"
        )
    with open(str(path), "r", encoding="utf-8") as f:
        data = yaml.safe_load(f) or {}
    if not isinstance(data, dict):
        raise ConfigError("%s 顶层必须是 mapping（key: value）" % path)
    return data


def available_profiles(config_dir=None):
    cdir = Path(config_dir) if config_dir else CONFIG_DIR
    return sorted(p.stem for p in cdir.glob("*.yaml") if p.stem != "base")


def load(profile=None, config_dir=None):
    """读 `base.yaml` → 叠加 `<profile>.yaml` → 校验，返回普通 dict。

    profile 为 None 时取 base.yaml 里的 `profile` 字段（默认 sim）。
    """
    cdir = Path(config_dir) if config_dir else CONFIG_DIR
    base_path = cdir / "base.yaml"
    if not base_path.exists():
        raise ConfigError("找不到配置文件：%s" % base_path)

    merged = _deep_merge(DEFAULTS, _read_yaml(base_path))
    name = profile or merged.get("profile") or "sim"
    profile_path = cdir / ("%s.yaml" % name)
    if not profile_path.exists():
        raise ConfigError(
            "未知 profile `%s`，可选：%s" % (name, "、".join(available_profiles(cdir)))
        )

    merged = _deep_merge(merged, _read_yaml(profile_path))
    merged["profile"] = name
    validate(merged)
    return merged


def _need(cond, message):
    if not cond:
        raise ConfigError(message)


def validate(cfg):
    """启动前的硬校验。宁可开不了机，也别跑到一半崩在演示现场。"""
    cam = cfg["camera"]
    _need(str(cam["backend"]) in _CAMERA_BACKENDS,
          "camera.backend=%r 不合法，可选 %s" % (cam["backend"], _CAMERA_BACKENDS))
    _need(int(cam["width"]) > 0 and int(cam["height"]) > 0,
          "camera.width/height 必须为正整数")
    _need(int(cam["warmup_frames"]) >= 0, "camera.warmup_frames 不能为负")

    roi = cfg["roi"]
    if roi["enabled"]:
        for key in ("x", "y", "w", "h"):
            _need(0.0 <= float(roi[key]) <= 1.0, "roi.%s 必须在 0~1 之间" % key)
        _need(float(roi["x"]) + float(roi["w"]) <= 1.0001, "roi.x + roi.w 不能超过 1")
        _need(float(roi["y"]) + float(roi["h"]) <= 1.0001, "roi.y + roi.h 不能超过 1")

    det = cfg["detection"]
    _need(str(det["backend"]) in _BACKEND_CHOICES["detection"],
          "detection.backend=%r 不合法，可选 %s"
          % (det["backend"], _BACKEND_CHOICES["detection"]))
    _need(isinstance(det.get("haar_equalize", True), bool),
          "detection.haar_equalize 必须是布尔值 true/false，当前 %r"
          % (det.get("haar_equalize"),))
    _need(0.0 < float(det["score_threshold"]) <= 1.0,
          "detection.score_threshold 必须在 (0, 1] 之间")
    _need(0.0 < float(det["nms_threshold"]) <= 1.0,
          "detection.nms_threshold 必须在 (0, 1] 之间")
    _need(float(det["min_face_px"]) >= 1, "detection.min_face_px 至少为 1 像素")

    rec = cfg["recognition"]
    _need(str(rec["backend"]) in _BACKEND_CHOICES["recognition"],
          "recognition.backend=%r 不合法，可选 %s"
          % (rec["backend"], _BACKEND_CHOICES["recognition"]))
    _need(0.0 < float(rec["match_threshold"]) <= 1.0,
          "recognition.match_threshold 必须在 (0, 1] 之间")
    _need(1 <= int(rec["confirm_frames"]) <= 30,
          "recognition.confirm_frames 应在 1~30 之间")
    _need(float(rec["cooldown_sec"]) >= 0, "recognition.cooldown_sec 不能为负")
    _need(float(rec["track_ttl_sec"]) > 0, "recognition.track_ttl_sec 必须为正")

    assoc = cfg["association"]
    _need(isinstance(assoc["enabled"], bool),
          "association.enabled 必须是布尔值 true/false，当前 %r" % (assoc["enabled"],))
    _need(0.0 < float(assoc["tau"]) <= 1.0, "association.tau 必须在 (0, 1] 之间")
    _need(float(assoc["margin"]) >= 0.0, "association.margin 不能为负")
    _need(float(assoc["quality_max_rms"]) >= 0.0, "association.quality_max_rms 不能为负")
    _need(float(assoc["session_ttl_sec"]) > 0, "association.session_ttl_sec 必须为正")
    _need(float(assoc["track_ttl_sec"]) > 0, "association.track_ttl_sec 必须为正")
    _need(int(assoc["max_sessions"]) >= 1, "association.max_sessions 至少为 1")
    _need(int(assoc["max_refs"]) >= 1, "association.max_refs 至少为 1")

    srv = cfg["server"]
    _need(str(srv["ws_url"]).startswith("ws://"),
          "server.ws_url 必须以 ws:// 开头（当前 %r）" % srv["ws_url"])
    _need(float(srv["heartbeat_sec"]) > 0, "server.heartbeat_sec 必须为正")

    out = cfg["output"]
    _need(str(out["snapshot_dir"]).strip() != "", "output.snapshot_dir 不能为空")
    _need(1 <= int(out["snapshot_quality"]) <= 100,
          "output.snapshot_quality 必须在 1~100 之间")

    runtime = cfg["runtime"]
    _need(float(runtime["max_fps"]) >= 0, "runtime.max_fps 不能为负")
    _need(str(runtime["backend"]) in _RUNTIME_BACKENDS,
          "runtime.backend=%r 不合法，可选 %s" % (runtime["backend"], _RUNTIME_BACKENDS))
    if str(runtime["backend"]) != "legacy":
        _need(str(runtime.get("detector_onnx", "")).strip() != "",
              "runtime.backend=%s 需要 runtime.detector_onnx 指向 YuNet ONNX"
              % runtime["backend"])
        _need(isinstance(runtime.get("warmup", True), bool),
              "runtime.warmup 必须是布尔值 true/false，当前 %r" % (runtime.get("warmup"),))
        _need(1 <= int(runtime.get("recognizer_batch", 8)) <= 8,
              "runtime.recognizer_batch 必须在 1~8 之间（SFace engine 的 profile 上限是 8）")

    assoc = cfg.get("association") or {}
    _need(isinstance(assoc.get("enabled", False), bool),
          "association.enabled 必须是布尔值 true/false，当前 %r"
          % (assoc.get("enabled"),))
    _need(0.0 < float(assoc.get("tau", 0.45)) <= 1.0,
          "association.tau 必须在 (0, 1] 之间，当前 %r" % (assoc.get("tau"),))
    _need(float(assoc.get("margin", 0.10)) >= 0.0, "association.margin 不能为负")
    _need(float(assoc.get("quality_max_rms", 0.10)) >= 0.0,
          "association.quality_max_rms 不能为负")


def resolve_vision(rel_path):
    """配置里的相对路径以 `vision/` 为根（模型、人脸库都在这里）。"""
    p = Path(str(rel_path))
    return str(p if p.is_absolute() else (VISION_DIR / p))


def resolve_project(rel_path):
    """快照目录等运行产物以仓库根为根（与 .gitignore 的写法对齐）。"""
    p = Path(str(rel_path))
    return str(p if p.is_absolute() else (PROJECT_DIR / p))
