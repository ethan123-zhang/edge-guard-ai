#!/usr/bin/env python3
"""人工重播循环压力探针：同一公开视频反复解码，复用生产检测/跟踪/协议到墙钟时长。

**这是人工重播压力测试，不是自然重入，也不是现场长期可靠性证据。**
循环边界由本工具人工拼接（EOF 后重开解码器），不产生人工真值，不判定自然遮挡、
真实重入或业务准确率；自然场景标注与事件预期由人工另行冻结。

做什么：
- 顺序解码一个本地公开视频；每次 EOF 只重开解码器，**不重置**检测器、FaceTracker、
  事件状态或累计时钟
- 媒体时间 = 累计解码序号 * 1000 / 源 FPS（显式 CFR），跨 EOF 严格递增；
  原片 frame_index 与 loop 同时记录，绝不把时间钳回源帧 0
- 按媒体 2Hz 网格采样；用进程内单调墙钟节拍逐个 tick 等待到 ``--duration-sec``，
  调度迟到如实统计（分桶分位数 + 总数/最大值），不把快进/慢放写成同一件事
- 真实 Haar 检测 → 原生 min_face_px 尺寸过滤 → 真实 FaceTracker → face_event 协议校验
- 周期采样 /proc/self/status 与 /proc/self/fd（RSS/FD/线程）；非 Linux 明确 unsupported，
  绝不伪造 0
- 异常与正常到时都在 finally 释放解码器并写出完整/部分 JSON；异常返回非零

不做：不调用摄像头、不联网、不读人脸库、不保存截图或任何身份信息、不修改生产算法。

用法（板端环境建议先导出 OPENBLAS_CORETYPE=ARMV8 等变量，见任务卡）：
    python3 scripts/video_stability_probe.py --video <mp4> --json <out.json> \\
        --duration-sec 600 --sample-fps 2 --time-source index-fps --profile jetson_local

退出码：0 = 跑满墙钟时长且无异常/协议错误；1 = 运行异常、协议错误或结果不完整；
2 = 参数或前置条件不满足（不会打开视频）。
"""
import argparse
import bisect
import json
import math
import os
import platform
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCRIPTS_DIR = os.path.dirname(os.path.abspath(__file__))
for _path in (ROOT, SCRIPTS_DIR):
    if _path not in sys.path:
        sys.path.insert(0, _path)

import video_pipeline_probe as _base

ProbeError = _base.ProbeError
EXIT_OK = _base.EXIT_OK
EXIT_PROBLEM = _base.EXIT_PROBLEM
EXIT_USAGE = _base.EXIT_USAGE
BASE_FRAME_TS_MS = _base.BASE_FRAME_TS_MS

SCHEMA = "sentinel-manager/video-stability-probe@1"
TASK_CARD = "docs/自然场景与稳定性任务卡-2026-11-01.md"

DEFAULT_DURATION_SEC = 600.0
DEFAULT_SAMPLE_FPS = 2.0
DEFAULT_PROFILE = "jetson_local"

# 资源观察窗口（任务卡的 Linux 验收口径；本工具只如实报告，不代替业务验收）
RESOURCE_WARMUP_SEC = 60.0
RSS_GROWTH_LIMIT_MIB = 64.0
FD_GROWTH_LIMIT = 2
THREAD_GROWTH_LIMIT = 4
RESOURCE_INTERVAL_SEC = 5.0
MAX_RESOURCE_SNAPSHOTS = 512

MAX_STORED_EVENTS = 200
SCHEDULE_LATE_MS = 1.0
# 实际墙钟窗口相对计划时长的判定容差（秒）：只容忍浮点/调度抖动，不容忍提前退出
WINDOW_TOLERANCE_SEC = 0.05

# 迟到分桶上界（毫秒）：只保留固定数量的桶，不积累逐帧原始值
LATENESS_BUCKET_UPPER_MS = (1.0, 2.0, 5.0, 10.0, 20.0, 50.0, 100.0,
                            250.0, 500.0, 1000.0, 2000.0, 5000.0)
LATENESS_PERCENTILES = (("p50", 0.50), ("p90", 0.90), ("p99", 0.99),
                        ("p999", 0.999))

# 本工具与它复用的生产逻辑都要能对得上源码 / 媒体 / 级联哈希
SOURCE_TARGETS = tuple(_base.SOURCE_TARGETS) + ("scripts/video_stability_probe.py",)


# ---------------------------------------------------------------- 参数与前置检查

def finite_positive(value, label):
    """有限正数；0、负数、NaN、inf、None、非数字一律 ProbeError。"""
    number = _base.finite_float(value)
    if number is None or number <= 0:
        raise ProbeError("%s 必须是有限正数（收到 %r）" % (label, value))
    return number


def require_positive_seconds(value, label="时长"):
    return finite_positive(value, label)


def positive_duration(value):
    """argparse type：把 ProbeError 翻译成命令行用法错误。"""
    try:
        return require_positive_seconds(value, "--duration-sec")
    except ProbeError as exc:
        raise argparse.ArgumentTypeError(str(exc))


def positive_sample_fps(value):
    try:
        return _base.require_positive_fps(value)
    except ProbeError as exc:
        raise argparse.ArgumentTypeError(str(exc))


def check_time_source(mode):
    """人工循环时钟只接受 index-fps：pts 会在每次重开解码器时跳回起点。"""
    text = str(mode or "").strip()
    if text not in _base.TIME_SOURCES:
        raise ProbeError("未知 --time-source=%r，可选 %s" % (mode, _base.TIME_SOURCES))
    if text != _base.TIME_SOURCE_INDEX_FPS:
        raise ProbeError(
            "--time-source 只支持 index-fps：人工拼接的循环媒体时间必须来自累计解码序号/fps；"
            "pts 在每次重开解码器时会跳回源片起点，不能当累计时钟用")
    return text


def check_video_input(path):
    """只接受现有本地视频文件：设备号、目录、不存在的路径都拒绝。"""
    text = str(path if path is not None else "").strip()
    if not text:
        raise ProbeError("--video 不能为空")
    if text.isdigit():
        raise ProbeError("--video 不能是设备号（%r）：本工具只接受现有本地视频文件" % text)
    if os.path.isdir(text):
        raise ProbeError("--video 是目录，不是视频文件：%s" % text)
    if not os.path.isfile(text):
        raise ProbeError("视频文件不存在：%s" % text)
    return os.path.abspath(text)


def require_source_fps(value):
    number = _base.finite_float(value)
    if number is None or number <= 0:
        raise ProbeError("视频源 FPS 无效（%r）：无法建立累计媒体时钟" % (value,))
    return number


def parse_args(argv=None):
    parser = argparse.ArgumentParser(
        description="人工重播循环压力测试（离线）：同一视频反复解码到墙钟时长，复用生产"
                    "检测/尺寸过滤/FaceTracker/face_event 协议。不冒充自然重入、"
                    "现场长期可靠性或人工真值。")
    parser.add_argument("--video", required=True,
                        help="现有本地视频文件（设备号/目录会被拒绝）")
    parser.add_argument("--json", dest="json_path", required=True,
                        help="JSON 输出路径（父目录必须已存在，解码前校验）")
    parser.add_argument("--duration-sec", dest="duration_sec", type=positive_duration,
                        default=DEFAULT_DURATION_SEC,
                        help="真实墙钟运行时长（秒），必须有限且>0，默认 600")
    parser.add_argument("--sample-fps", dest="sample_fps", type=positive_sample_fps,
                        default=DEFAULT_SAMPLE_FPS,
                        help="媒体时间网格采样率，默认 2")
    parser.add_argument("--time-source", dest="time_source", choices=_base.TIME_SOURCES,
                        default=_base.TIME_SOURCE_INDEX_FPS,
                        help="只支持 index-fps：累计解码序号/fps 的人工循环媒体时钟")
    parser.add_argument("--profile", default=DEFAULT_PROFILE,
                        help="配置 profile，默认 jetson_local")
    return parser.parse_args(argv)


def source_hashes():
    out = {}
    for relative in SOURCE_TARGETS:
        absolute = os.path.join(ROOT, relative.replace("/", os.sep))
        out[relative] = _base.sha256_file(absolute) if os.path.isfile(absolute) else None
    return out


# ---------------------------------------------------------------- 累计媒体时钟

class CumulativeMediaClock(object):
    """人工循环媒体时钟：media_ms = 累计解码序号 * 1000 / fps。

    累计序号跨 EOF 继续递增，所以媒体时间严格递增；原片 frame_index 与 loop 分开记录。
    这里既不加"上一帧+1ms"，也不读 POS_MSEC（重开解码器后 PTS 会回到起点）。
    """

    mode = _base.TIME_SOURCE_INDEX_FPS
    contract = "cumulative_index_fps_over_artificial_loops"

    def __init__(self, fps):
        self.fps = require_source_fps(fps)
        self.frame_ms = 1000.0 / self.fps
        self.frames = 0
        self.loops = 0
        self.last_ms = None

    def observe(self, source_frame_index):
        cumulative = self.frames
        media_ms = cumulative * self.frame_ms
        if self.last_ms is not None and media_ms <= self.last_ms:
            raise ProbeError(
                "累计媒体时间没有前进：累计帧 %d -> %.3f ms，上一帧 %.3f ms"
                % (cumulative, media_ms, self.last_ms))
        self.last_ms = media_ms
        self.frames += 1
        return {"cumulative_frame_index": cumulative,
                "source_frame_index": int(source_frame_index),
                "loop_index": int(self.loops),
                "media_ms": media_ms}

    def report(self):
        return {
            "mode": self.mode,
            "contract": self.contract,
            "fps": self.fps,
            "frame_ms": self.frame_ms,
            "frames_observed": self.frames,
            "loops_observed": self.loops,
            "last_media_ms": self.last_ms,
            "note": "媒体时间来自累计解码序号，跨 EOF 不回退；与真实墙钟时长严格分开",
        }


# ---------------------------------------------------------------- 墙钟节拍与迟到

class LatenessHistogram(object):
    """固定分桶的迟到分布：只保留桶计数，不无限积累逐帧原始值。"""

    def __init__(self):
        self.upper_ms = LATENESS_BUCKET_UPPER_MS
        self.counts = [0] * (len(self.upper_ms) + 1)
        self.total = 0
        self.sum_ms = 0.0
        self.max_ms = 0.0

    def record(self, late_ms):
        value = _base.finite_float(late_ms)
        if value is None or value < 0:
            value = 0.0
        self.total += 1
        self.sum_ms += value
        if value > self.max_ms:
            self.max_ms = value
        self.counts[bisect.bisect_right(self.upper_ms, value)] += 1

    def _label(self, index):
        if index <= 0:
            return "0-1ms"
        if index >= len(self.upper_ms):
            return ">=%gms" % self.upper_ms[-1]
        return "%g-%gms" % (self.upper_ms[index - 1], self.upper_ms[index])

    def _upper(self, index):
        if index >= len(self.upper_ms):
            return self.upper_ms[-1]
        return self.upper_ms[index]

    def percentiles(self):
        out = {}
        for name, _quantile in LATENESS_PERCENTILES:
            out[name] = None
        if not self.total:
            return out
        for name, quantile in LATENESS_PERCENTILES:
            target = int(math.ceil(quantile * self.total))
            seen = 0
            chosen = len(self.counts) - 1
            for index, count in enumerate(self.counts):
                seen += count
                if seen >= target:
                    chosen = index
                    break
            out[name] = self._upper(chosen)
        return out

    def report(self):
        buckets = [{"range": self._label(index), "count": count}
                   for index, count in enumerate(self.counts) if count]
        return {
            "count": self.total,
            "mean_ms": round(self.sum_ms / self.total, 3) if self.total else None,
            "max_ms": round(self.max_ms, 3),
            "buckets": buckets,
            "percentiles_ms": self.percentiles(),
            "approximation": "分位数由固定毫秒分桶的上界估计，不保留逐帧迟到原始值",
        }


class MonotonicPacer(object):
    """进程内单调墙钟节拍：第 n 个 tick 的目标时刻 = origin + n / rate。

    每个 tick 真正等待到目标时刻；处理本身超时就如实记迟到，不补睡、不攒到最后。
    """

    def __init__(self, rate_hz, monotonic=None, sleep=None):
        self.rate_hz = finite_positive(rate_hz, "节拍频率")
        self.interval_sec = 1.0 / self.rate_hz
        self._monotonic = monotonic or time.monotonic
        self._sleep = sleep or time.sleep
        self.origin_sec = self._monotonic()
        self.ticks = 0
        self.late_ticks = 0
        self.late_ms_total = 0.0
        self.late_ms_max = 0.0
        self.sleep_calls = 0
        self.sleep_ms_total = 0.0
        self.histogram = LatenessHistogram()
        self.last_tick = None
        self.deadline_sec = None
        self.window_end_sec = None
        self.final_wait_ms = 0.0

    def target_sec(self, tick):
        return self.origin_sec + int(tick) * self.interval_sec

    @property
    def next_target_sec(self):
        return self.target_sec(self.ticks)

    def wait(self):
        tick = self.ticks
        target = self.target_sec(tick)
        now = self._monotonic()
        slept = 0.0
        if now < target:
            slept = target - now
            self._sleep(slept)
            self.sleep_calls += 1
            self.sleep_ms_total += slept * 1000.0
            now = self._monotonic()
        late_ms = max(0.0, (now - target) * 1000.0)
        self.late_ms_total += late_ms
        if late_ms > self.late_ms_max:
            self.late_ms_max = late_ms
        if late_ms > SCHEDULE_LATE_MS:
            self.late_ticks += 1
        self.histogram.record(late_ms)
        self.ticks += 1
        self.last_tick = {"tick": tick, "target_sec": target, "wake_sec": now,
                          "late_ms": late_ms, "sleep_ms": slept * 1000.0}
        return self.last_tick

    def finish(self, deadline_sec):
        """跑满墙钟窗口：只有不足的剩余时长才等待，不通过额外观测凑时间。"""
        self.deadline_sec = float(deadline_sec)
        now = self._monotonic()
        remaining = self.deadline_sec - now
        waited_ms = 0.0
        if remaining > 0:
            self._sleep(remaining)
            self.sleep_calls += 1
            self.sleep_ms_total += remaining * 1000.0
            waited_ms = remaining * 1000.0
        self.final_wait_ms = waited_ms
        self.window_end_sec = self._monotonic()
        return {"waited_ms": round(waited_ms, 3),
                "window_sec": self.window_end_sec - self.origin_sec,
                "deadline_sec": self.deadline_sec}

    def report(self):
        total = self.ticks
        return {
            "rate_hz": self.rate_hz,
            "interval_ms": round(self.interval_sec * 1000.0, 6),
            "ticks": total,
            "late_ticks": self.late_ticks,
            "late_threshold_ms": SCHEDULE_LATE_MS,
            "late_fraction": round(float(self.late_ticks) / total, 6) if total else None,
            "late_ms_total": round(self.late_ms_total, 3),
            "late_ms_max": round(self.late_ms_max, 3),
            "sleep_calls": self.sleep_calls,
            "sleep_ms_total": round(self.sleep_ms_total, 3),
            "final_wait_ms": round(self.final_wait_ms, 3),
            "window_sec": None if self.window_end_sec is None
                          else round(self.window_end_sec - self.origin_sec, 6),
            "lateness": self.histogram.report(),
            "last_tick": self.last_tick,
            "note": "按进程内单调墙钟逐 tick 等待；迟到分位数只作调度诊断，"
                    "不替代资源/业务验收",
        }


# ---------------------------------------------------------------- 解码器生命周期

class LoopDecoder(object):
    """只管理解码器的打开/重开/释放；检测器、跟踪器、时钟都不在这里。"""

    def __init__(self, reader_factory, path):
        self._factory = reader_factory
        self.path = str(path)
        self.reader = None
        self.opens = 0
        self.reads = 0

    def open(self):
        self.reader = self._factory(self.path)
        self.opens += 1
        return self.reader

    def reopen(self):
        self.close()
        return self.open()

    def read(self):
        if self.reader is None:
            raise ProbeError("解码器未打开")
        self.reads += 1
        return self.reader.read()

    def meta(self):
        if self.reader is None:
            return {}
        meta = getattr(self.reader, "meta", None)
        return meta() if meta is not None else {}

    def close(self):
        reader = self.reader
        self.reader = None
        if reader is None:
            return
        release = getattr(reader, "release", None)
        if release is not None:
            release()


def _video_meta(meta):
    out = {}
    if not isinstance(meta, dict):
        return out
    for key in ("width", "height", "fps_header", "frame_count_header",
                "duration_s_header", "fourcc"):
        if key in meta:
            out[key] = meta[key]
    return out


# ---------------------------------------------------------------- 资源观察

def parse_proc_status(text):
    """解析 /proc/self/status 里我们关心的两行；缺字段返回 None（不伪造 0）。"""
    values = {}
    for line in str(text).splitlines():
        key, separator, rest = line.partition(":")
        if not separator:
            continue
        parts = rest.split()
        if not parts:
            continue
        try:
            values[key.strip()] = int(parts[0])
        except ValueError:
            continue
    rss_kb = values.get("VmRSS")
    return {
        "rss_kb": rss_kb,
        "rss_mib": None if rss_kb is None else round(rss_kb / 1024.0, 6),
        "threads": values.get("Threads"),
    }


def read_resources(proc_root="/proc"):
    """读一次进程资源快照；非 Linux / procfs 不可用时明确 unsupported。"""
    root = str(proc_root)
    status_path = os.path.join(root, "self", "status")
    snapshot = {"supported": False, "source": "procfs", "reason": None,
                "rss_kb": None, "rss_mib": None, "threads": None, "fd_count": None}
    if not os.path.isfile(status_path):
        snapshot["reason"] = ("找不到 %s（非 Linux 或 procfs 未挂载）："
                              "资源监测 unsupported，不伪造 0" % status_path)
        return snapshot
    try:
        with open(status_path, "r", encoding="utf-8", errors="replace") as handle:
            parsed = parse_proc_status(handle.read())
    except (IOError, OSError) as exc:
        snapshot["reason"] = "读取 %s 失败：%s" % (status_path, exc)
        return snapshot
    snapshot.update(parsed)
    snapshot["supported"] = True
    fd_dir = os.path.join(root, "self", "fd")
    if os.path.isdir(fd_dir):
        try:
            snapshot["fd_count"] = len(os.listdir(fd_dir))
        except OSError:
            snapshot["fd_count"] = None
    return snapshot


RESOURCE_FIELDS = ("rss_mib", "fd_count", "threads")


def _valid_resource_value(value):
    """资源字段必须是有限非负数；真实 0 有效，缺失/NaN/inf/负数/字符串无效。"""
    number = _base.finite_float(value)
    if number is None or number < 0:
        return None
    return number


def _invalid_resource_fields(snapshot):
    return [key for key in RESOURCE_FIELDS
            if _valid_resource_value(snapshot.get(key)) is None]


def resource_verdict(snapshots, warmup_sec, rss_limit_mib, fd_limit, thread_limit):
    """按"预热后增长"口径给资源观察结论；无法判断时 ok 必须是 None，不伪通过。"""
    limits = {"rss_growth_mib": float(rss_limit_mib),
              "fd_growth": int(fd_limit),
              "thread_growth": int(thread_limit)}
    usable = [item for item in (snapshots or []) if item.get("supported")]
    if not usable:
        return {"status": "unsupported", "ok": None,
                "reason": "非 Linux / procfs 不可用：资源监测 unsupported，不伪造 0",
                "limits": limits}
    baseline = None
    for item in usable:
        if float(item.get("elapsed_sec") or 0.0) >= float(warmup_sec):
            baseline = item
            break
    final = usable[-1]
    if baseline is None:
        return {"status": "insufficient_warmup", "ok": None,
                "warmup_sec": float(warmup_sec), "final": dict(final),
                "limits": limits,
                "note": "本次运行没有达到预热时长，资源增长不下结论"}
    baseline_bad = _invalid_resource_fields(baseline)
    final_bad = _invalid_resource_fields(final)
    if baseline_bad or final_bad:
        return {"status": "insufficient_data", "ok": None,
                "warmup_sec": float(warmup_sec),
                "baseline": dict(baseline), "final": dict(final),
                "invalid_fields": {"baseline": baseline_bad, "final": final_bad},
                "limits": limits,
                "note": "基线/末次快照缺少有效数值：无法判断资源增长，不伪通过"}
    growth = {
        "rss_mib": _delta(final["rss_mib"], baseline["rss_mib"]),
        "fd": _delta(final["fd_count"], baseline["fd_count"]),
        "threads": _delta(final["threads"], baseline["threads"]),
    }
    exceeded = []
    if growth["rss_mib"] is not None and growth["rss_mib"] > limits["rss_growth_mib"]:
        exceeded.append("rss")
    if growth["fd"] is not None and growth["fd"] > limits["fd_growth"]:
        exceeded.append("fd")
    if growth["threads"] is not None and growth["threads"] > limits["thread_growth"]:
        exceeded.append("threads")
    return {"status": "ok" if not exceeded else "growth_exceeded",
            "ok": not exceeded,
            "warmup_sec": float(warmup_sec),
            "baseline": dict(baseline),
            "final": dict(final),
            "growth": growth,
            "limits": limits,
            "exceeded": exceeded}


def _delta(final, baseline):
    if final is None or baseline is None:
        return None
    return round(float(final) - float(baseline), 3)


class ResourceMonitor(object):
    """周期资源快照：容量有限，满了抽稀并放大采样间隔，保持时间覆盖。"""

    def __init__(self, proc_root="/proc", warmup_sec=RESOURCE_WARMUP_SEC,
                 rss_limit_mib=RSS_GROWTH_LIMIT_MIB, fd_limit=FD_GROWTH_LIMIT,
                 thread_limit=THREAD_GROWTH_LIMIT, interval_sec=RESOURCE_INTERVAL_SEC,
                 max_snapshots=MAX_RESOURCE_SNAPSHOTS):
        self.proc_root = str(proc_root)
        self.warmup_sec = float(warmup_sec)
        self.rss_limit_mib = float(rss_limit_mib)
        self.fd_limit = int(fd_limit)
        self.thread_limit = int(thread_limit)
        self.interval_sec = finite_positive(interval_sec, "资源采样间隔")
        self.max_snapshots = max(2, int(max_snapshots))
        self.snapshots = []
        self.decimations = 0
        self.next_at_sec = 0.0
        probe = read_resources(self.proc_root)
        self.supported = bool(probe.get("supported"))
        self.reason = probe.get("reason")

    def observe(self, elapsed_sec):
        snapshot = read_resources(self.proc_root)
        snapshot["elapsed_sec"] = round(float(elapsed_sec), 3)
        self._append(snapshot)
        return snapshot

    def _append(self, snapshot):
        if len(self.snapshots) >= self.max_snapshots:
            # 容量到顶：隔一个丢一个并把采样间隔翻倍，覆盖范围不缩水
            self.snapshots = self.snapshots[::2]
            self.interval_sec *= 2.0
            self.decimations += 1
        self.snapshots.append(snapshot)

    def maybe_observe(self, elapsed_sec):
        if not self.supported:
            return None
        elapsed = float(elapsed_sec)
        if self.snapshots and elapsed < self.next_at_sec:
            return None
        snapshot = self.observe(elapsed)
        self.next_at_sec = elapsed + self.interval_sec
        return snapshot

    def report(self):
        verdict = resource_verdict(self.snapshots, self.warmup_sec,
                                   self.rss_limit_mib, self.fd_limit,
                                   self.thread_limit)
        return {
            "supported": self.supported,
            "reason": self.reason,
            "platform": platform.system(),
            "proc_root": self.proc_root,
            "warmup_sec": self.warmup_sec,
            "sample_interval_sec": round(self.interval_sec, 3),
            "max_snapshots": self.max_snapshots,
            "snapshot_count": len(self.snapshots),
            "decimations": self.decimations,
            "limits": {"rss_growth_mib": self.rss_limit_mib,
                       "fd_growth": self.fd_limit,
                       "thread_growth": self.thread_limit},
            "snapshots": [dict(item) for item in self.snapshots],
            "verdict": verdict,
            "note": "资源快照只作观察，不无限积累原始帧或逐帧元数据；"
                    "判定与退出码分离，资源结论不替代业务验收",
        }


# ---------------------------------------------------------------- 生产组件注入

class ProbeDeps(object):
    """一次运行需要的全部受控依赖；测试可整体替换，生产走 build_probe_deps。"""

    def __init__(self, reader_factory, detector, tracker_obj, min_face_px=80,
                 events_module=None, monotonic=None, sleep=None,
                 resource_monitor=None, cv2_version=None):
        self.reader_factory = reader_factory
        self.detector = detector
        self.tracker_obj = tracker_obj
        self.min_face_px = int(min_face_px)
        self.events_module = events_module
        self.monotonic = monotonic or time.monotonic
        self.sleep = sleep or time.sleep
        self.resource_monitor = resource_monitor
        self.cv2_version = cv2_version

    @property
    def events(self):
        if self.events_module is None:
            from vision import events as vision_events
            self.events_module = vision_events
        return self.events_module


def build_probe_deps(payload, profile):
    """真实运行：现有配置 + 真实检测器 + 真实 FaceTracker + 真实视频解码器。"""
    import cv2
    from vision import events as vision_events
    from vision import tracker as vision_tracker

    version = getattr(cv2, "__version__", None)
    payload["opencv"] = {"version": version}
    cfg = _base.load_config(profile)
    detector_obj, min_face_px = _base.build_detector(cfg, payload)
    tracker_obj = vision_tracker.FaceTracker(
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
        "recognition": {"used": False,
                        "reason": "压力探针不做识别：face_id/name 为 None、score 固定 0.0"},
    }
    return ProbeDeps(
        reader_factory=_base.OpenCVVideoReader,
        detector=detector_obj,
        tracker_obj=tracker_obj,
        min_face_px=min_face_px,
        events_module=vision_events,
        resource_monitor=ResourceMonitor(),
        cv2_version=version,
    )


# ---------------------------------------------------------------- 主循环

def run_decode_loop(decoder, clock, sampler, pacer, duration_sec, handle_sample,
                    monotonic, resource_monitor=None, on_sample=None, stats=None):
    """顺序解码 + 人工循环 + 2Hz 媒体采样 + 单调墙钟节拍，直到 duration_sec。

    - EOF 只重开解码器（``decoder.reopen()``），时钟/检测器/跟踪器由调用方持有
    - 每次 EOF 核对本圈实际解码帧数与第一圈一致；零帧或变长循环直接失败
    - 每个墙钟 tick 至少产出一个采样；处理慢时迟到如实记录，不补帧
    - ``stats`` 由调用方传入并原地更新：途中异常时已处理进度不会归零
    - 结束后只等待不足的剩余时长，并以实际墙钟窗口是否跑满作为完整性条件
    """
    if stats is None:
        stats = {}
    stats.update({"decoded_frames": int(stats.get("decoded_frames") or 0),
                  "sampled_frames": int(stats.get("sampled_frames") or 0),
                  "skipped_frames": int(stats.get("skipped_frames") or 0),
                  "eofs": int(stats.get("eofs") or 0),
                  "loops_completed": int(stats.get("loops_completed") or 0),
                  "frames_in_first_loop": stats.get("frames_in_first_loop"),
                  "read_ms_total": float(stats.get("read_ms_total") or 0.0),
                  "read_ms_max": float(stats.get("read_ms_max") or 0.0),
                  "stop_reason": stats.get("stop_reason"),
                  "window_complete": bool(stats.get("window_complete"))})
    deadline = pacer.origin_sec + float(duration_sec)
    loop_frames = 0
    source_index = 0
    while True:
        if pacer.next_target_sec > deadline + 1e-9:
            stats["stop_reason"] = "duration"
            break
        pacer.wait()
        while True:
            read_started = monotonic()
            ok, frame = decoder.read()
            read_ms = (monotonic() - read_started) * 1000.0
            stats["read_ms_total"] += read_ms
            if read_ms > stats["read_ms_max"]:
                stats["read_ms_max"] = read_ms
            if not ok or frame is None:
                stats["eofs"] += 1
                stats["loops_completed"] += 1
                if loop_frames <= 0:
                    raise ProbeError(
                        "循环解码在累计帧 %d 遇到 0 帧 EOF：视频为空或解码失败"
                        % stats["decoded_frames"])
                if stats["frames_in_first_loop"] is None:
                    stats["frames_in_first_loop"] = loop_frames
                elif loop_frames != stats["frames_in_first_loop"]:
                    raise ProbeError(
                        "循环解码长度变化：第 1 圈 %d 帧，第 %d 圈 %d 帧"
                        "（每次重开解码器都必须解出相同帧数）"
                        % (stats["frames_in_first_loop"],
                           stats["loops_completed"], loop_frames))
                loop_frames = 0
                source_index = 0
                decoder.reopen()                       # 只重开解码器
                fps_now = require_source_fps(decoder.meta().get("fps_header"))
                if abs(fps_now - clock.fps) > 1e-6:
                    raise ProbeError("重开解码器后 FPS 变化：%.6f -> %.6f"
                                     % (clock.fps, fps_now))
                clock.loops += 1
                continue
            observed = clock.observe(source_index)
            stats["decoded_frames"] += 1
            loop_frames += 1
            source_index += 1
            media_ms = observed["media_ms"]
            if sampler.due(media_ms) is None:
                stats["skipped_frames"] += 1
                continue
            tick = sampler.consume(media_ms)
            stats["sampled_frames"] += 1
            handle_sample(observed, tick, frame)
            if on_sample is not None:
                on_sample(observed, tick, frame)
            break
        if resource_monitor is not None:
            resource_monitor.maybe_observe(monotonic() - pacer.origin_sec)
        if monotonic() >= deadline:
            stats["stop_reason"] = "duration"
            break
    if stats["stop_reason"] is None:
        stats["stop_reason"] = "duration"
    if stats["stop_reason"] == "duration":
        # 最后一个 tick 之后到截止时刻之间的剩余时长照实等满；不额外处理窗口外样本
        pacer.finish(deadline)
        window_sec = pacer.window_end_sec - pacer.origin_sec
        stats["wall_window_sec"] = window_sec
        stats["window_complete"] = bool(
            window_sec >= float(duration_sec) - WINDOW_TOLERANCE_SEC)
    return stats


# ---------------------------------------------------------------- 输出

def new_payload(args, json_path, video_path, duration_sec, sample_fps, time_source):
    return {
        "schema": SCHEMA,
        "task_card": TASK_CARD,
        "generated_at_local": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "scope": {
            "kind": "offline_artificial_loop_stress",
            "label": "人工重播循环压力测试",
            "is_natural_reentry": False,
            "is_field_long_run": False,
            "produces_ground_truth": False,
            "does": [
                "重复解码同一本地公开视频，复用生产检测、min_face_px 尺寸过滤、"
                "FaceTracker 与 face_event 协议校验",
                "用累计解码序号/fps 建立跨 EOF 的人工循环媒体时钟，按 2Hz 媒体网格采样",
                "用进程内单调墙钟节拍跑满 --duration-sec，记录调度迟到与周期资源快照",
            ],
            "does_not": [
                "不冒充自然遮挡或真实重入：循环边界是人工拼接，不是现场事件",
                "不判定业务准确率、不生成人工真值；自然场景标注与事件预期由人工另行冻结",
                "不调用摄像头、不联网、不读人脸库、不保存截图或身份信息",
                "不作为现场长期可靠性证据：资源阈值只是本进程的观察窗口",
            ],
        },
        "params": {
            "video": video_path,
            "json": json_path,
            "duration_sec": float(duration_sec),
            "sample_fps": float(sample_fps),
            "time_source": str(time_source),
            "profile": str(args.profile),
        },
        "interpreter": {
            "executable": sys.executable,
            "version": "%d.%d.%d" % sys.version_info[:3],
            "implementation": platform.python_implementation(),
        },
        "opencv": {"version": None},
        "platform": {"system": platform.system(),
                     "release": platform.release(),
                     "machine": platform.machine()},
        "sources": source_hashes(),
        "video": {"path": video_path},
        "detector": None,
        "config": {},
        "clock": {},
        "sampling": {},
        "decode": {},
        "detection": {},
        "tracker": {},
        "events": {},
        "pacing": {},
        "resources": {},
        "run": {},
        "warnings": [],
        "errors": [],
        "partial": False,
        "ok": False,
        "stop_reason": None,
    }


def tracker_report(tracker_obj):
    if tracker_obj is None:
        return {"available": False, "final_track_count": 0, "final_tracks": [],
                "note": "运行在创建跟踪器之前失败"}
    tracks = _base.tracker_state(tracker_obj) if hasattr(tracker_obj, "_tracks") else []
    return {
        "available": True,
        "confirm_frames": int(getattr(tracker_obj, "confirm_frames", 0) or 0),
        "cooldown_sec": round(float(getattr(tracker_obj, "cooldown_ms", 0)) / 1000.0, 3),
        "track_ttl_sec": round(float(getattr(tracker_obj, "ttl_ms", 0)) / 1000.0, 3),
        "final_track_count": len(tracks),
        "final_next_track_id": int(getattr(tracker_obj, "_next_track_id", 0) or 0),
        "final_tracks": tracks,
        "note": "全程只有一个跟踪器实例；循环只重开解码器，"
                "不重置轨道/冷却/事件状态",
    }


def finalize(payload, deps, state):
    clock = state.get("clock")
    pacer = state.get("pacer")
    sampler = state.get("sampler")
    stats = state.get("stats") or {}
    counters = state.get("counters") or {}
    duration_sec = float(state.get("duration_sec") or 0.0)
    elapsed_sec = state.get("elapsed_sec")

    payload["clock"] = clock.report() if clock is not None else {
        "mode": _base.TIME_SOURCE_INDEX_FPS,
        "contract": CumulativeMediaClock.contract,
        "fps": None,
        "frames_observed": 0,
        "note": "未建立累计媒体时钟（运行在解码前失败）",
    }
    payload["sampling"] = {
        "sample_fps": state.get("sample_fps"),
        "step_ms": None if sampler is None else round(sampler.step_ms, 6),
        "samples": int(stats.get("sampled_frames") or 0),
        "ticks_skipped": int(stats.get("ticks_skipped") or 0),
        "last_grid_ms": None if sampler is None else sampler.last_tick_ms,
        "last_sample": state.get("last_sample"),
    }
    decoded = int(stats.get("decoded_frames") or 0)
    first_loop = stats.get("frames_in_first_loop")
    header = payload["video"].get("frame_count_header")
    applicable = bool(header and first_loop is not None)
    equal = None
    if applicable:
        equal = int(header) == int(first_loop)
        if not equal:
            payload["warnings"].append(
                "声明 %d 帧，首圈实际解码 %d 帧：以实测为准，EOF 可信度受限"
                % (int(header), int(first_loop)))
    payload["decode"] = {
        "decoded_frames_total": decoded,
        "sampled_frames": int(stats.get("sampled_frames") or 0),
        "skipped_frames": int(stats.get("skipped_frames") or 0),
        "loops_completed": int(stats.get("loops_completed") or 0),
        "eofs": int(stats.get("eofs") or 0),
        "frames_in_first_loop": first_loop,
        "decoder_opens": int(state.get("decoder_opens") or 0),
        "read_ms_total": round(float(stats.get("read_ms_total") or 0.0), 3),
        "read_ms_max": round(float(stats.get("read_ms_max") or 0.0), 3),
        "frame_count_check": {
            "declared": header,
            "first_loop_decoded": first_loop,
            "applicable": applicable,
            "equal": equal,
            "note": "循环长度一致性以每圈实测帧数互相比对为准；声明帧数只作警告",
        },
    }
    payload["detection"] = {
        "samples": int(counters.get("samples") or 0),
        "detections_total": int(counters.get("detected") or 0),
        "kept_total": int(counters.get("kept") or 0),
        "filtered_small_total": int(counters.get("filtered_small") or 0),
        "detect_ms_total": round(float(counters.get("detect_ms_total") or 0.0), 3),
        "detect_ms_max": round(float(counters.get("detect_ms_max") or 0.0), 3),
        "track_ms_total": round(float(counters.get("track_ms_total") or 0.0), 3),
        "track_ms_max": round(float(counters.get("track_ms_max") or 0.0), 3),
        "min_face_px": None if deps is None else int(deps.min_face_px),
    }
    stored = state.get("event_store") or []
    event_count = int(state.get("event_count") or 0)
    event_problems = int(state.get("event_problems") or 0)
    payload["events"] = {
        "count": event_count,
        "protocol_violations": event_problems,
        "stored": stored,
        "stored_limit": MAX_STORED_EVENTS,
        "dropped": max(0, event_count - len(stored)),
        "note": "只保留前 %d 条事件明细；协议违规按全量事件累计"
                % MAX_STORED_EVENTS,
    }
    payload["tracker"] = tracker_report(None if deps is None else deps.tracker_obj)
    payload["pacing"] = pacer.report() if pacer is not None else {
        "rate_hz": None, "ticks": 0, "late_ticks": 0,
        "lateness": {"count": 0, "percentiles_ms": {}},
        "note": "未建立节拍器（运行在解码前失败）",
    }
    monitor = None if deps is None else deps.resource_monitor
    payload["resources"] = monitor.report() if monitor is not None else {
        "supported": False,
        "reason": "未创建资源监测器：资源监测 unsupported，不伪造 0",
        "snapshots": [],
        "snapshot_count": 0,
        "decimations": 0,
        "limits": {"rss_growth_mib": RSS_GROWTH_LIMIT_MIB,
                   "fd_growth": FD_GROWTH_LIMIT,
                   "thread_growth": THREAD_GROWTH_LIMIT},
        "verdict": {"status": "unsupported", "ok": None,
                    "reason": "未创建资源监测器"},
    }

    media_sec = None
    if clock is not None and clock.last_ms is not None:
        media_sec = round(clock.last_ms / 1000.0, 3)
    wall_sec = None if elapsed_sec is None else round(float(elapsed_sec), 3)
    wall_window_sec = stats.get("wall_window_sec")
    window_complete = bool(stats.get("window_complete"))
    stop_reason = "error" if payload["partial"] else stats.get("stop_reason")
    payload["run"] = {
        "started_local": state.get("started_local"),
        "finished_local": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "planned_wall_sec": round(duration_sec, 3),
        "actual_wall_sec": wall_sec,
        "wall_window_sec": None if wall_window_sec is None
                           else round(float(wall_window_sec), 3),
        "window_complete": window_complete,
        "media_sec": media_sec,
        "media_vs_wall": {
            "wall_sec": wall_sec,
            "media_sec": media_sec,
            "media_over_wall": None if not wall_sec else round(media_sec / wall_sec, 6),
            "note": "media_sec 是人工拼接循环累计的视频时间，actual_wall_sec 是真实"
                    "进程耗时；二者分开报告，不把快进/慢放写成同一件事",
        },
        "stop_reason": stop_reason,
    }
    payload["stop_reason"] = stop_reason
    payload["ok"] = bool(
        not payload["partial"]
        and not payload["errors"]
        and event_problems == 0
        and decoded > 0
        and payload["decode"]["sampled_frames"] > 0
        and stop_reason == "duration"
        and window_complete
    )


# ---------------------------------------------------------------- 主流程

def run_probe(args, deps=None, on_sample=None):
    # 参数、输入输出路径全部先校验：任何错误都不应该打开解码器
    json_path = _base.check_json_output(getattr(args, "json_path", None))
    video_path = check_video_input(getattr(args, "video", None))
    duration_sec = require_positive_seconds(getattr(args, "duration_sec", None),
                                            "--duration-sec")
    sample_fps = _base.require_positive_fps(getattr(args, "sample_fps", None))
    time_source = check_time_source(getattr(args, "time_source", None))
    profile = str(getattr(args, "profile", "") or "").strip()
    if not profile:
        raise ProbeError("--profile 不能为空")

    payload = new_payload(args, json_path, video_path, duration_sec, sample_fps,
                          time_source)
    state = {
        "clock": None, "pacer": None, "sampler": None,
        # 与 run_decode_loop 共享同一个 dict：handle_sample 途中抛异常时，
        # 已解码/已采样的真实进度仍会写进部分 JSON
        "stats": {"decoded_frames": 0, "sampled_frames": 0, "skipped_frames": 0,
                  "eofs": 0, "loops_completed": 0, "frames_in_first_loop": None,
                  "read_ms_total": 0.0, "read_ms_max": 0.0, "stop_reason": None,
                  "wall_window_sec": None, "window_complete": False},
        "counters": {"samples": 0, "detected": 0, "kept": 0, "filtered_small": 0,
                     "detect_ms_total": 0.0, "detect_ms_max": 0.0,
                     "track_ms_total": 0.0, "track_ms_max": 0.0},
        "last_sample": None, "event_store": [], "event_count": 0,
        "event_problems": 0, "duration_sec": duration_sec,
        "elapsed_sec": None, "started_local": None,
        "decoder_opens": 0, "sample_fps": sample_fps,
    }
    counters = state["counters"]

    decoder = None
    started = None
    exit_code = EXIT_OK
    try:
        if deps is None:
            deps = build_probe_deps(payload, profile)
        payload["opencv"]["version"] = getattr(deps, "cv2_version", None)

        started = deps.monotonic()
        state["started_local"] = time.strftime("%Y-%m-%dT%H:%M:%S")
        decoder = LoopDecoder(deps.reader_factory, video_path)
        decoder.open()
        payload["video"].update(_video_meta(decoder.meta()))
        payload["video"]["size_bytes"] = os.path.getsize(video_path)
        payload["video"]["sha256"] = _base.sha256_file(video_path)

        fps = require_source_fps(payload["video"].get("fps_header"))
        clock = CumulativeMediaClock(fps)
        state["clock"] = clock
        sampler = _base.GridSampler(sample_fps)
        state["sampler"] = sampler
        pacer = MonotonicPacer(sample_fps, monotonic=deps.monotonic, sleep=deps.sleep)
        state["pacer"] = pacer
        min_face_px = int(getattr(deps, "min_face_px", 0) or 0)

        def handle_sample(observed, tick, frame):
            loop_index = int(observed["loop_index"])
            cumulative = int(observed["cumulative_frame_index"])
            source_index = int(observed["source_frame_index"])
            media_ms = float(observed["media_ms"])
            frame_ts = _base.frame_ts_from_media(media_ms)
            counters["samples"] += 1

            detect_started = deps.monotonic()
            try:
                detections = list(deps.detector.detect(frame))
            except Exception as exc:
                raise ProbeError(
                    "检测异常（%s: %s）在第 %d 圈累计帧 %d：不能静默当作 0 框"
                    % (type(exc).__name__, exc, loop_index, cumulative))
            detect_ms = (deps.monotonic() - detect_started) * 1000.0
            counters["detect_ms_total"] += detect_ms
            if detect_ms > counters["detect_ms_max"]:
                counters["detect_ms_max"] = detect_ms
            counters["detected"] += len(detections)
            kept = [item for item in detections if int(item.size) >= min_face_px]
            filtered_small = len(detections) - len(kept)
            counters["kept"] += len(kept)
            counters["filtered_small"] += filtered_small
            faces = [{"bbox": item.bbox, "face_id": None, "name": None,
                      "score": 0.0} for item in kept]

            track_started = deps.monotonic()
            try:
                track_events = list(deps.tracker_obj.update(faces, frame_ts))
            except Exception as exc:
                raise ProbeError(
                    "跟踪异常（%s: %s）在第 %d 圈累计帧 %d"
                    % (type(exc).__name__, exc, loop_index, cumulative))
            track_ms = (deps.monotonic() - track_started) * 1000.0
            counters["track_ms_total"] += track_ms
            if track_ms > counters["track_ms_max"]:
                counters["track_ms_max"] = track_ms

            emitted = 0
            for event in track_events:
                try:
                    msg = deps.events.face_event(
                        event.face_id, event.is_known, event.name, event.confidence,
                        event.bbox, event.frame_ts, frame_ts)
                    problems = list(deps.events.check(msg))
                except Exception as exc:
                    raise ProbeError(
                        "事件协议校验异常（%s: %s）在第 %d 圈累计帧 %d"
                        % (type(exc).__name__, exc, loop_index, cumulative))
                state["event_count"] += 1
                state["event_problems"] += len(problems)
                emitted += 1
                if len(state["event_store"]) < MAX_STORED_EVENTS:
                    state["event_store"].append({
                        "loop_index": loop_index,
                        "cumulative_frame_index": cumulative,
                        "source_frame_index": source_index,
                        "media_ms": round(media_ms, 3),
                        "frame_ts": frame_ts,
                        "track_id": int(event.track_id),
                        "face_event": msg,
                        "problems": problems,
                    })
            state["last_sample"] = {
                "loop_index": loop_index,
                "cumulative_frame_index": cumulative,
                "source_frame_index": source_index,
                "media_ms": round(media_ms, 3),
                "frame_ts": frame_ts,
                "grid_tick_ms": round(float(tick), 3),
                "detected": len(detections),
                "kept": len(kept),
                "filtered_small": filtered_small,
                "events": emitted,
                "detect_ms": round(detect_ms, 3),
                "track_ms": round(track_ms, 3),
            }

        run_decode_loop(
            decoder, clock, sampler, pacer, duration_sec, handle_sample,
            deps.monotonic, resource_monitor=deps.resource_monitor,
            on_sample=on_sample, stats=state["stats"])
    except Exception as exc:
        payload["partial"] = True
        payload["errors"].append("%s: %s" % (type(exc).__name__, exc))
        exit_code = EXIT_PROBLEM
    finally:
        if decoder is not None:
            try:
                decoder.close()
            except Exception as exc:
                payload["warnings"].append("释放解码器失败：%s" % (exc,))
            state["decoder_opens"] = decoder.opens
        if started is not None and deps is not None:
            state["elapsed_sec"] = deps.monotonic() - started
        finalize(payload, deps, state)
        try:
            _base.write_json(json_path, payload)
        except Exception as exc:
            sys.stderr.write("[video-stability] 写 JSON 失败：%s\n" % (exc,))
            if exit_code == EXIT_OK:
                exit_code = EXIT_PROBLEM

    if not payload["ok"] and exit_code == EXIT_OK:
        exit_code = EXIT_PROBLEM
    return exit_code


def main(argv=None):
    args = parse_args(argv)
    try:
        return run_probe(args)
    except ProbeError as exc:
        sys.stderr.write("[video-stability] %s\n" % (exc,))
        return EXIT_USAGE


if __name__ == "__main__":
    sys.exit(main())
