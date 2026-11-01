#!/usr/bin/env python3
"""公开视频离线回归探针：单次顺序解码 + 视频时间网格抽样 + 生产检测/跟踪/协议。

用途：没有摄像头配合时，用公开视频验证「检测 → 尺寸过滤 → FaceTracker →
face_event 协议」这条链路，并留下可复现的逐帧证据。

只做这些：读一个本地视频、按2Hz网格抽样、调用生产 detector / FaceTracker / events、
把元数据和事件写进 JSON。**不做**身份识别、不读人脸库、不存截图、不联网、不开摄像头。
它不冒充完整 VisionService，也不产出人工真值。

时间约定（最容易踩的地方）：
- 媒体时间只来自视频本身；处理墙钟不参与任何 TTL / 事件判定
- 默认 ``--time-source pts``：首选 POS_MSEC，无效或不前进时用 frame_index/fps 回退
- ``--time-source index-fps``：显式 CFR 假设，媒体时间 = frame_index*1000/fps。
  不读 POS_MSEC 做时间、也不做「减一帧」补偿：OpenCV 4.1.1 实测 POS_MSEC 是下一帧
  时刻（2026-11-01 板端审查），所以原始 POS_MSEC 只作诊断留档

用法：
    python3 scripts/video_pipeline_probe.py --video <mp4> --json <out.json> \\
        [--sample-fps 2] [--start 0] [--end 44] \\
        [--profile jetson_local] [--time-source pts|index-fps]

退出码：0 = 抽样完整且无协议/时间错误（不代表准确率合格）；1 = 运行/协议/时间问题；
2 = 参数或前置条件不满足。
"""
import argparse
import hashlib
import json
import math
import os
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

BASE_FRAME_TS_MS = 1700000000000

TIME_SOURCE_PTS = "pts"
TIME_SOURCE_INDEX_FPS = "index-fps"
TIME_SOURCES = (TIME_SOURCE_PTS, TIME_SOURCE_INDEX_FPS)

# 媒体时间来源标签（写进每个样本，便于事后分辨"这帧的时间是谁给的"）
SRC_PTS = "pts"
SRC_INDEX_FALLBACK = "fallback_index_fps"
SRC_INDEX_CFR = "explicit_cfr_index_fps"
CONTRACT_PTS = "pts_preferred"

EXIT_OK = 0
EXIT_PROBLEM = 1
EXIT_USAGE = 2

DEFAULT_HAAR = "/usr/share/opencv4/haarcascades/haarcascade_frontalface_default.xml"

# 只对这几个文件算哈希：它们是本探针用到的生产逻辑，结果要能对得上源码
SOURCE_TARGETS = (
    "scripts/video_pipeline_probe.py",
    "vision/config.py",
    "vision/detector.py",
    "vision/events.py",
    "vision/tracker.py",
)


class ProbeError(RuntimeError):
    """探针自身的可预期失败（路径不对、级联缺失、时间合同被破坏……）。"""


# ---------------------------------------------------------------- 参数与前置检查

def finite_float(value):
    """转成有限浮点；None/NaN/inf/非数字一律返回 None（JSON 里不能出现 NaN）。"""
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(number):
        return None
    return number


def require_positive_fps(value):
    """采样率：必须是有限正数（0、负数、NaN、inf 全部拒绝）。"""
    number = finite_float(value)
    if number is None or number <= 0:
        raise ProbeError("采样率必须是有限正数（收到 %r）" % (value,))
    return number


def positive_fps(value):
    """argparse type：把 ProbeError 翻译成命令行用法错误。"""
    try:
        return require_positive_fps(value)
    except ProbeError as exc:
        raise argparse.ArgumentTypeError(str(exc))


def seconds(value):
    """argparse type：--start / --end 的有限非负秒数。"""
    number = finite_float(value)
    if number is None or number < 0:
        raise argparse.ArgumentTypeError("秒数必须是有限非负数（收到 %r）" % (value,))
    return number


def check_window(start_s, end_s):
    """--end 可选，但给了就必须大于 --start。"""
    if end_s is not None and float(end_s) <= float(start_s):
        raise ProbeError("--end 必须大于 --start（start=%r end=%r）" % (start_s, end_s))


def check_json_output(path):
    """输出父目录必须先存在；在打开视频之前检查，避免白解码一场。"""
    if not str(path or "").strip():
        raise ProbeError("--json 不能为空")
    target = os.path.abspath(str(path))
    parent = os.path.dirname(target)
    if not os.path.isdir(parent):
        raise ProbeError("JSON 父目录不存在：%s" % parent)
    return target


def parse_args(argv=None):
    parser = argparse.ArgumentParser(
        description="公开视频离线回归探针（检测/尺寸过滤/跟踪/协议，不做身份识别）")
    parser.add_argument("--video", required=True, help="本地视频文件（必须存在）")
    parser.add_argument("--json", dest="json_path", required=True,
                        help="JSON 输出路径（父目录必须已存在）")
    parser.add_argument("--sample-fps", dest="sample_fps", type=positive_fps,
                        default=2.0, help="视频时间网格采样率，默认 2")
    parser.add_argument("--start", type=seconds, default=0.0, help="窗口起点（秒），默认 0")
    parser.add_argument("--end", type=seconds, default=None,
                        help="窗口终点（秒），可选，必须大于 --start")
    parser.add_argument("--profile", default="jetson_local", help="配置 profile，默认 jetson_local")
    parser.add_argument("--time-source", dest="time_source", choices=TIME_SOURCES,
                        default=TIME_SOURCE_PTS,
                        help="媒体时间来源：pts=首选 POS_MSEC（默认）；"
                             "index-fps=显式 CFR，frame_index*1000/fps")
    return parser.parse_args(argv)


# ---------------------------------------------------------------- 媒体时间

def frame_ts_from_media(media_ms):
    """frame_ts = 1700000000000 + round(media_ms)，与协议里的毫秒时间戳对齐。"""
    return BASE_FRAME_TS_MS + int(round(float(media_ms)))


class MediaClock(object):
    """把不可信的 POS_MSEC 变成严格递增的媒体时间（纯逻辑，不依赖 cv2）。

    - ``pts`` 模式：合法首帧 0 必须保留；无效（None/NaN/inf/负）或不前进的 PTS
      回退到 ``frame_index*1000/fps``，来源与回退次数都记账
    - ``index-fps`` 模式：只用 ``frame_index*1000/fps``（显式 CFR），
      POS_MSEC 只作为 ``pts_ms`` 诊断字段保留
    - 回退值本身不前进（无法安全回退）时**直接失败**：绝不把时间钳到"上一帧 +1ms"
      伪造单调性，那会掩盖 TTL 误判。失败时保留 partial 结果。
    """

    def __init__(self, fps, mode=TIME_SOURCE_PTS):
        if mode not in TIME_SOURCES:
            raise ProbeError("未知媒体时间来源 %r，可选 %s" % (mode, TIME_SOURCES))
        self.mode = mode
        self.fps = finite_float(fps)
        if self.fps is not None and self.fps <= 0:
            self.fps = None
        if mode == TIME_SOURCE_INDEX_FPS and self.fps is None:
            raise ProbeError("--time-source index-fps 需要有效的视频 FPS（收到 %r）" % (fps,))
        self.last_ms = None
        self.frames = 0
        self.pts_used = 0
        self.fallback = 0
        self.pts_invalid = 0
        self.violations = 0
        self.first_pts_ms = None
        self.first_media_ms = None

    def observe(self, frame_index, pos_msec):
        """返回这一帧的媒体时间与来源（dict，直接可写进 JSON）。"""
        pts = finite_float(pos_msec)
        status = "ok"
        if pts is None or pts < 0:
            pts = None
            status = "invalid"
            self.pts_invalid += 1
        elif self.first_pts_ms is None:
            self.first_pts_ms = pts

        if self.mode == TIME_SOURCE_INDEX_FPS:
            media = self._from_index(frame_index)
            source = SRC_INDEX_CFR
        elif pts is not None and (self.last_ms is None or pts > self.last_ms):
            media = pts
            source = SRC_PTS
            self.pts_used += 1
        else:
            media = self._from_index(frame_index)
            source = SRC_INDEX_FALLBACK
            self.fallback += 1

        if self.last_ms is not None and media <= self.last_ms:
            # 回退值不前进：宁可失败，也不把时间钳成"上一帧 +1ms"（那是伪造媒体时间）
            self.violations += 1
            raise ProbeError(
                "媒体时间无法安全回退：帧 %d 的回退值 %.3f ms 不大于上一帧 %.3f ms"
                "（--time-source %s，PTS=%r）"
                % (frame_index, media, self.last_ms, self.mode, pos_msec))

        self.last_ms = media
        self.frames += 1
        if self.first_media_ms is None:
            self.first_media_ms = media
        return {"frame_index": int(frame_index), "media_ms": media, "source": source,
                "pts_ms": pts, "pts_status": status}

    def _from_index(self, frame_index):
        if self.fps is None:
            raise ProbeError("FPS 无效且 PTS 不可用：无法用 frame_index/fps 回退")
        return int(frame_index) * 1000.0 / self.fps

    def report(self):
        return {
            "mode": self.mode,
            "contract": SRC_INDEX_CFR if self.mode == TIME_SOURCE_INDEX_FPS else CONTRACT_PTS,
            "fps_for_clock": self.fps,
            "frames_observed": self.frames,
            "first_pts_ms": self.first_pts_ms,
            "first_media_ms": self.first_media_ms,
            "last_media_ms": self.last_ms,
            "pts_used": self.pts_used,
            "fallback_used": self.fallback,
            "pts_invalid": self.pts_invalid,
            "monotonic_violations": self.violations,
        }


# ---------------------------------------------------------------- 网格抽样

class GridSampler(object):
    """视频时间网格（默认 2Hz）：只有落在网格点上的源帧才进检测器/跟踪器。

    网格点算的是**全局** ``k*step``（不是每帧累加），所以不会漂移；``--start``
    只是跳过起点之前的网格点，不会让采样相位随窗口移动。
    """

    def __init__(self, sample_fps, start_s=0.0, end_s=None):
        self.sample_fps = require_positive_fps(sample_fps)
        self.step_ms = 1000.0 / self.sample_fps
        self.start_ms = float(start_s) * 1000.0
        self.end_ms = None if end_s is None else float(end_s) * 1000.0
        check_window(start_s, end_s)
        self._index = int(math.ceil(self.start_ms / self.step_ms - 1e-9))
        self.sampled = 0
        self.ticks_skipped = 0
        self.first_tick_ms = None
        self.last_tick_ms = None
        self._stop_past_end()

    def _tick(self, index):
        return index * self.step_ms

    def _stop_past_end(self):
        if self.end_ms is not None and self._tick(self._index) >= self.end_ms - 1e-9:
            self._index = None

    @property
    def next_tick_ms(self):
        return None if self._index is None else self._tick(self._index)

    def due(self, media_ms):
        """这一帧是否到达下一个网格点；到了就返回该网格点毫秒，否则 None。"""
        tick = self.next_tick_ms
        if tick is None or float(media_ms) < tick:
            return None
        return tick

    def consume(self, media_ms):
        """记一次采样，并把指针推到第一个大于本帧媒体时间的网格点。"""
        tick = self.next_tick_ms
        if tick is None:
            return None
        self.sampled += 1
        if self.first_tick_ms is None:
            self.first_tick_ms = tick
        self.last_tick_ms = tick
        media_ms = float(media_ms)
        self._index += 1
        while self.next_tick_ms is not None and self.next_tick_ms <= media_ms:
            # 帧间隔大于网格间隔时（丢帧/PTS 跳变），中间的网格点没有帧可用
            self.ticks_skipped += 1
            self._index += 1
        self._stop_past_end()
        return tick

    def report(self):
        return {
            "sampled_frames": self.sampled,
            "ticks_skipped": self.ticks_skipped,
            "first_grid_ms": self.first_tick_ms,
            "last_grid_ms": self.last_tick_ms,
            "next_grid_ms": self.next_tick_ms,
        }


# ---------------------------------------------------------------- 解码循环

def decode_and_sample(reader, clock, sampler, on_sample):
    """单次顺序解码到 EOF（或被 ``--end`` 截断）；不循环、不 sleep。

    ``reader`` 只需要两个方法：``read() -> (ok, frame)`` 和 ``pos_msec()``。
    只有落在网格点上的帧才调用 ``on_sample``；被跳过的源帧不进检测器/跟踪器。
    EOF 之后立刻 break，不会再读一次（跳过 EOF 的循环会把视频放两遍）。
    """
    stats = {"decoded_frames": 0, "sampled_frames": 0, "skipped_frames": 0,
             "eof": False, "stop_reason": None, "decode_ms_total": 0.0}
    frame_index = 0
    while True:
        read_started = time.perf_counter()
        ok, frame = reader.read()
        read_ms = (time.perf_counter() - read_started) * 1000.0
        if not ok or frame is None:
            stats["eof"] = True
            stats["stop_reason"] = "eof"
            break
        observed = clock.observe(frame_index, reader.pos_msec())
        observed["decode_ms"] = read_ms
        stats["decoded_frames"] += 1
        stats["decode_ms_total"] += read_ms
        if sampler.end_ms is not None and observed["media_ms"] >= sampler.end_ms:
            stats["stop_reason"] = "end"
            break
        tick = sampler.due(observed["media_ms"])
        if tick is None:
            stats["skipped_frames"] += 1
        else:
            sampler.consume(observed["media_ms"])
            stats["sampled_frames"] += 1
            on_sample(observed, tick, frame)
        frame_index += 1
    stats["ticks_skipped"] = sampler.ticks_skipped
    return stats


# ---------------------------------------------------------------- 视频源

class OpenCVVideoReader(object):
    """cv2 延迟导入：本模块的纯函数测试不需要 OpenCV。"""

    def __init__(self, path):
        import cv2
        self._cv2 = cv2
        self.path = str(path)
        self.cap = cv2.VideoCapture(self.path)
        if not self.cap.isOpened():
            raise ProbeError("视频打不开：%s（路径或编码不被 OpenCV 支持？）" % self.path)

    def read(self):
        ok, frame = self.cap.read()
        return bool(ok), frame

    def pos_msec(self):
        return self.cap.get(self._cv2.CAP_PROP_POS_MSEC)

    def meta(self):
        cv2, cap = self._cv2, self.cap
        fps = finite_float(cap.get(cv2.CAP_PROP_FPS))
        count = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
        return {
            "width": int(cap.get(cv2.CAP_PROP_FRAME_WIDTH)),
            "height": int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT)),
            "fps_header": fps,
            "frame_count_header": count,
            "duration_s_header": round(count / fps, 3) if (fps and count > 0) else None,
            "fourcc": int(cap.get(cv2.CAP_PROP_FOURCC)),
        }

    def release(self):
        if self.cap is not None:
            try:
                self.cap.release()
            finally:
                self.cap = None


def sha256_file(path, chunk_size=1 << 20):
    digest = hashlib.sha256()
    with open(str(path), "rb") as handle:
        while True:
            block = handle.read(chunk_size)
            if not block:
                break
            digest.update(block)
    return digest.hexdigest()


def source_hashes():
    """记录所用生产逻辑与本脚本的源码哈希（结果要能和源码对上）。"""
    out = {}
    for relative in SOURCE_TARGETS:
        absolute = os.path.join(ROOT, relative.replace("/", os.sep))
        out[relative] = sha256_file(absolute) if os.path.isfile(absolute) else None
    return out


# ---------------------------------------------------------------- 生产组件

def resolve_cascade(cv2, configured):
    """Haar XML 定位：配置路径 → 板端默认 → OpenCV 自带（PC 上只有后者）。"""
    candidates = []
    if configured:
        candidates.append(str(configured))
    candidates.append(DEFAULT_HAAR)
    data = getattr(cv2, "data", None)
    if data is not None:
        candidates.append(os.path.join(getattr(data, "haarcascades", ""),
                                       "haarcascade_frontalface_default.xml"))
    for path in candidates:
        if path and os.path.isfile(path):
            return path
    raise ProbeError("找不到 Haar XML（试过：%s）" % "；".join(p for p in candidates if p))


def load_config(profile):
    from vision import config
    try:
        return config.load(profile)
    except config.ConfigError as exc:
        raise ProbeError("配置加载失败：%s" % exc)


def build_detector(cfg, payload):
    """按现有配置造生产检测器；级联路径换了就明说（不静默换口径）。"""
    import cv2
    from vision import config, detector

    det_cfg = cfg["detection"]
    backend = str(det_cfg["backend"])
    if backend == "auto":
        backend = "yunet" if os.path.exists(config.resolve_vision(det_cfg["model"])) else "haar"

    cascade_configured = str(det_cfg.get("haar_cascade") or "")
    cascade_used = None
    if backend == "haar":
        cascade_used = resolve_cascade(cv2, cascade_configured)
        det_cfg["haar_cascade"] = cascade_used
        if cascade_configured and os.path.abspath(cascade_configured) != os.path.abspath(cascade_used):
            payload["warnings"].append(
                "配置的 Haar 级联不存在（%s），改用 %s；检测口径随之变化"
                % (cascade_configured, cascade_used))
    try:
        det = detector.create_detector(cfg)
    except detector.DetectorUnavailable as exc:
        raise ProbeError("检测器不可用：%s" % exc)
    payload["detector"] = {
        "backend": getattr(det, "backend", "?"),
        "describe": det.describe() if hasattr(det, "describe") else None,
        "cascade_configured": cascade_configured or None,
        "cascade_used": cascade_used,
        "cascade_sha256": sha256_file(cascade_used) if cascade_used else None,
        "min_face_px": int(det_cfg["min_face_px"]),
        "score_threshold": float(det_cfg["score_threshold"]),
    }
    return det, int(det_cfg["min_face_px"])


def tracker_state(face_tracker):
    """只读轨道快照（id/bbox/last_ts/votes/emitted），给「轨道前后状态」留证。"""
    return [{"id": int(track.id), "bbox": list(track.bbox), "last_ts": int(track.last_ts),
             "votes": dict(track.votes), "emitted": bool(track.emitted)}
            for track in face_tracker._tracks]


# ---------------------------------------------------------------- 输出

def new_payload(args, json_path):
    return {
        "schema": "sentinel-manager/public-video-pipeline-probe@1",
        "task_card": "docs/公开视频回归任务卡-2026-11-01.md",
        "generated_at_local": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "params": {
            "video": os.path.abspath(str(args.video)),
            "json": json_path,
            "sample_fps": float(args.sample_fps),
            "start_s": float(args.start),
            "end_s": None if args.end is None else float(args.end),
            "profile": str(args.profile),
            "time_source": str(args.time_source),
        },
        "limits": [
            "只验证检测/尺寸过滤/跟踪/协议，不冒充完整 VisionService，不做身份识别",
            "不读人脸库、不存截图、不发网络、不调用摄像头",
            "退出码 0 只代表抽样完整且无协议/时间错误，不代表准确率合格",
            "人工标注与模型结果必须分开记录；本探针不产出真值，也不放大画面跨 80px 门槛",
        ],
        "video": {"path": os.path.abspath(str(args.video))},
        "sampling": {},
        "time": {"mode": str(args.time_source), "frame_ts_base_ms": BASE_FRAME_TS_MS},
        "sources": source_hashes(),
        "config": {},
        "detector": None,
        "decode": {},
        "samples": [],
        "events": [],
        "event_count": 0,
        "event_protocol_violations": 0,
        "timing": {},
        "warnings": [],
        "errors": [],
        "stop_reason": None,
        "partial": False,
        "ok": False,
    }


def write_json(path, payload):
    with open(str(path), "w", encoding="utf-8") as handle:
        json.dump(payload, handle, ensure_ascii=False, indent=2)
        handle.write("\n")


# ---------------------------------------------------------------- 主流程

def run(args):
    json_path = check_json_output(args.json_path)   # 打开视频之前
    check_window(args.start, args.end)
    if not os.path.isfile(str(args.video)):
        raise ProbeError("视频文件不存在：%s" % args.video)

    from vision import events, tracker

    payload = new_payload(args, json_path)
    reader = None
    clock = None
    started = time.perf_counter()
    exit_code = EXIT_OK
    try:
        reader = OpenCVVideoReader(args.video)
        payload["video"].update(reader.meta())
        payload["video"]["size_bytes"] = os.path.getsize(str(args.video))
        payload["video"]["sha256"] = sha256_file(str(args.video))

        clock = MediaClock(payload["video"]["fps_header"], args.time_source)
        sampler = GridSampler(args.sample_fps, args.start, args.end)
        payload["sampling"] = {
            "sample_fps": sampler.sample_fps,
            "step_ms": round(sampler.step_ms, 6),
            "start_s": sampler.start_ms / 1000.0,
            "end_s": None if sampler.end_ms is None else sampler.end_ms / 1000.0,
            "first_grid_ms": sampler.next_tick_ms,
        }

        cfg = load_config(args.profile)
        detector_obj, min_face_px = build_detector(cfg, payload)
        tracker_obj = tracker.FaceTracker(
            cfg["recognition"]["confirm_frames"],
            cfg["recognition"]["cooldown_sec"],
            cfg["recognition"]["track_ttl_sec"],
        )
        payload["config"] = {
            "profile": cfg["profile"],
            "detection": {key: cfg["detection"][key] for key in (
                "backend", "score_threshold", "nms_threshold", "top_k",
                "min_face_px", "haar_min_neighbors")},
            "haar_equalize": bool(cfg["detection"].get("haar_equalize", True)),
            "tracker": {key: cfg["recognition"][key] for key in (
                "confirm_frames", "cooldown_sec", "track_ttl_sec")},
            "recognition": {"used": False, "reason": "本探针不做识别：face_id/name 为 None、"
                                                     "score 固定 0.0（与 recognition.backend=none 一致）"},
        }

        def on_sample(observed, tick, frame):
            record = {
                "frame_index": observed["frame_index"],
                "media_ms": round(observed["media_ms"], 3),
                "frame_ts": frame_ts_from_media(observed["media_ms"]),
                "time_source": observed["source"],
                "pts_ms": observed["pts_ms"],
                "pts_status": observed["pts_status"],
                "grid_tick_ms": round(tick, 3),
                "decode_ms": round(observed["decode_ms"], 3),
                "detect_ms": 0.0,
                "track_ms": 0.0,
                "frame_shape": [int(value) for value in frame.shape],
                "detected": 0,
                "kept": 0,
                "filtered_small": 0,
                "boxes": [],
                "kept_boxes": [],
                "tracks_before": [],
                "tracks_after": [],
                "events": [],
            }
            detect_started = time.perf_counter()
            try:
                detections = detector_obj.detect(frame)
            except Exception as exc:   # 检测异常不能静默当 0 框
                raise ProbeError("检测异常（%s: %s）在帧 %d：不能静默当作 0 框"
                                 % (type(exc).__name__, exc, observed["frame_index"]))
            record["detect_ms"] = round((time.perf_counter() - detect_started) * 1000.0, 3)
            record["detected"] = len(detections)
            record["boxes"] = [{"bbox": list(det.bbox), "size": int(det.size),
                                "score": round(float(det.score), 3)} for det in detections]
            kept = [det for det in detections if det.size >= min_face_px]
            record["kept"] = len(kept)
            record["filtered_small"] = len(detections) - len(kept)
            record["kept_boxes"] = [{"bbox": list(det.bbox), "size": int(det.size)}
                                    for det in kept]
            faces = [{"bbox": det.bbox, "face_id": None, "name": None, "score": 0.0}
                     for det in kept]

            record["tracks_before"] = tracker_state(tracker_obj)
            track_started = time.perf_counter()
            track_events = tracker_obj.update(faces, record["frame_ts"])
            record["track_ms"] = round((time.perf_counter() - track_started) * 1000.0, 3)
            record["tracks_after"] = tracker_state(tracker_obj)

            for event in track_events:
                # ts 用 frame_ts：离线探针没有真实发送时刻，别把处理墙钟混进协议字段
                msg = events.face_event(
                    event.face_id, event.is_known, event.name, event.confidence,
                    event.bbox, event.frame_ts, record["frame_ts"])
                problems = events.check(msg)
                item = {"frame_index": record["frame_index"],
                        "media_ms": record["media_ms"],
                        "frame_ts": record["frame_ts"],
                        "track_id": int(event.track_id),
                        "face_event": msg,
                        "problems": problems}
                record["events"].append({"track_id": int(event.track_id),
                                         "face_event": msg, "problems": problems})
                payload["events"].append(item)
            payload["samples"].append(record)
            payload["event_count"] = len(payload["events"])

        stats = decode_and_sample(reader, clock, sampler, on_sample)
        payload["decode"] = dict(stats)
        payload["stop_reason"] = stats["stop_reason"]
        payload["sampling"].update(sampler.report())

        declared = payload["video"]["frame_count_header"]
        payload["decode"]["frame_count_check"] = {
            "declared": declared,
            "decoded": stats["decoded_frames"],
            "applicable": stats["stop_reason"] == "eof",
            "note": None if stats["stop_reason"] == "eof"
                    else "按 --end 截断，声明帧数不与本次解码帧数比较",
        }
        if stats["stop_reason"] == "eof" and declared > 0:
            payload["decode"]["frame_count_check"]["equal"] = (
                declared == stats["decoded_frames"])
            if declared != stats["decoded_frames"]:
                payload["warnings"].append(
                    "声明 %d 帧，实际解码 %d 帧：EOF 可信度受限"
                    % (declared, stats["decoded_frames"]))
        if stats["stop_reason"] == "end":
            payload["warnings"].append(
                "按 --end=%s 截断：未解码到 EOF，EOF 可信度不适用于本次结果" % args.end)
        elif args.end is not None:
            payload["warnings"].append(
                "视频在 --end=%s 之前就 EOF：窗口实际只覆盖到 %.3f 秒"
                % (args.end, (clock.last_ms or 0.0) / 1000.0))
    except Exception as exc:
        payload["partial"] = True
        payload["errors"].append("%s: %s" % (type(exc).__name__, exc))
        payload["stop_reason"] = "error"
        exit_code = EXIT_PROBLEM
    finally:
        if reader is not None:
            reader.release()
        finalize(payload, clock, time.perf_counter() - started)
        write_json(json_path, payload)
    if not payload["ok"] and exit_code == EXIT_OK:
        exit_code = EXIT_PROBLEM   # 协议/完整性在 finalize 里判掉的问题不能伪装成成功
    return exit_code


def finalize(payload, clock, elapsed_sec):
    samples = payload["samples"]
    decoded = int(payload.get("decode", {}).get("decoded_frames") or 0)
    payload["timing"] = {
        "process_wall_ms": round(elapsed_sec * 1000.0, 3),
        "note": "处理墙钟只作诊断，不参与媒体时间、TTL 或事件判定",
        "decode_ms_total": round(sum(item["decode_ms"] for item in samples), 3),
        "detect_ms_total": round(sum(item["detect_ms"] for item in samples), 3),
        "track_ms_total": round(sum(item["track_ms"] for item in samples), 3),
        "sampled_frames": len(samples),
    }
    if clock is not None:
        payload["time"].update(clock.report())
        if (payload["time"]["mode"] == TIME_SOURCE_PTS and samples
                and clock.pts_used == 0 and clock.fallback > 0):
            payload["warnings"].append("POS_MSEC 全部不可用：媒体时间来自 frame_index/fps 回退")

    # 零解码 / 零采样不是"抽样完整"：--start 超出片长这类情况必须明确失败
    if not payload["errors"]:
        if decoded == 0:
            payload["errors"].append("一帧都没解码出来：视频为空或解码失败")
        elif not samples:
            payload["errors"].append(
                "0 个采样：--start/--end 与视频媒体时间没有交集（start 可能超出片长）")

    violations = sum(len(item["problems"]) for item in payload["events"])
    payload["event_protocol_violations"] = violations
    payload["ok"] = bool(
        exit_code_is_clean(payload)
        and violations == 0
        and payload["stop_reason"] in ("eof", "end")
        and int(payload.get("time", {}).get("monotonic_violations", 0)) == 0
        and decoded > 0
        and bool(samples)
    )


def exit_code_is_clean(payload):
    return not payload["partial"] and not payload["errors"]


def main(argv=None):
    args = parse_args(argv)
    try:
        code = run(args)
    except ProbeError as exc:
        sys.stderr.write("[video-probe] %s\n" % exc)
        return EXIT_USAGE
    return code


if __name__ == "__main__":
    sys.exit(main())
