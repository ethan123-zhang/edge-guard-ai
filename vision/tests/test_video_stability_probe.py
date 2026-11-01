"""人工重播循环压力探针的受控测试（不读真实视频、不 import cv2、不联网、不等 600 秒）。

钉住的合同（对应 docs/自然场景与稳定性任务卡-2026-11-01.md）：

- 时长 / 采样率 / 源 FPS 的 0、负数、NaN、inf 全部拒绝；参数错误先于打开视频
- 12fps 源跨 EOF 后累计 frame_index/fps 继续递增，2Hz 采样不跳回源帧 0 的媒体时刻
- 循环只重开解码器：真实 FaceTracker 的轨道 / 冷却 / emitted 状态跨循环保留，
  同一未知空间轨道不会因为重播再发一次事件
- 零帧、循环解码长度变化明确失败；异常与正常到时都释放解码器并写出 partial / errors
- 真实节拍是逐 tick 等待，不是最后一次性 sleep；调度迟到如实计入分位数
- Linux 资源解析 / 非 Linux unsupported；资源快照容量有限；事件协议失败返回非零

全部使用假解码器 + 假检测器 + 可控时钟，因此 PC 没装 OpenCV、板端 OpenCV 4.1.1
都能跑，也不需要真的等 600 秒。
"""
import argparse
import ast
import contextlib
import hashlib
import importlib.util
import io
import json
import math
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
import uuid

PROJECT_DIR = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
SCRIPTS_DIR = os.path.join(PROJECT_DIR, "scripts")
SCRIPT_PATH = os.path.join(SCRIPTS_DIR, "video_stability_probe.py")
# 本机沙箱只允许写工作区，且用 os.makedirs 建的目录才可写：
# 临时文件放项目忽略目录，用完立刻删，不污染仓库
SCRATCH_ROOT = os.path.join(PROJECT_DIR, "logs", "video-validation")
for _path in (PROJECT_DIR, SCRIPTS_DIR):
    if _path not in sys.path:
        sys.path.insert(0, _path)


@contextlib.contextmanager
def scratch_dir(prefix="stability-test-"):
    """工作区内的临时目录（平台 TEMP / mkdtemp 在受限环境里可能不可写）。"""
    os.makedirs(SCRATCH_ROOT, exist_ok=True)
    path = os.path.join(SCRATCH_ROOT, "%s%s" % (prefix, uuid.uuid4().hex[:12]))
    os.makedirs(path)
    try:
        yield path
    finally:
        shutil.rmtree(path, ignore_errors=True)

from vision import events as real_events          # noqa: E402
from vision import tracker as real_tracker        # noqa: E402


def _load_probe():
    """按文件路径加载新探针；它内部延迟 import cv2 / vision，所以这里不需要 OpenCV。"""
    spec = importlib.util.spec_from_file_location("video_stability_probe", SCRIPT_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


probe = _load_probe()

FAKE_VIDEO_BYTES = b"fake-video-bytes"


# ---------------------------------------------------------------- 受控替身

class FakeClock(object):
    """可控单调时钟：sleep 精确推进 now，用来验证真实节拍而不是真的等。"""

    def __init__(self, start=1000.0):
        self.now = float(start)
        self.sleeps = []

    def monotonic(self):
        return self.now

    def sleep(self, seconds):
        seconds = float(seconds)
        if seconds < 0:
            raise AssertionError("节拍器不应 sleep 负数：%r" % (seconds,))
        self.sleeps.append(seconds)
        self.now += seconds

    def advance(self, seconds):
        self.now += float(seconds)


class FakeDetection(object):
    def __init__(self, bbox, score=0.9):
        self.bbox = tuple(int(value) for value in bbox)
        self.score = float(score)
        self.size = min(self.bbox[2], self.bbox[3])


class FakeDetector(object):
    def __init__(self, boxes=((10, 10, 120, 120),), clock=None, work_sec=0.0):
        self.boxes = [tuple(box) for box in boxes]
        self.calls = 0
        self.clock = clock
        self.work_sec = float(work_sec)

    def detect(self, frame):
        self.calls += 1
        if self.clock is not None and self.work_sec:
            self.clock.advance(self.work_sec)
        return [FakeDetection(box) for box in self.boxes]


class ExplodingDetector(object):
    def detect(self, frame):
        raise RuntimeError("detector boom")


class FlakyDetector(object):
    """前 N 次正常，第 N+1 次抛异常：用来验证途中失败仍保留已处理进度。"""

    def __init__(self, fail_on=3):
        self.fail_on = int(fail_on)
        self.calls = 0

    def detect(self, frame):
        self.calls += 1
        if self.calls == self.fail_on:
            raise RuntimeError("detector boom on sample %d" % self.calls)
        return [FakeDetection((10, 10, 120, 120))]


class BrokenEvents(object):
    """协议永远不合规：用来证明事件协议失败会返回非零。"""

    def __init__(self):
        self.checked = 0

    def face_event(self, *args, **kwargs):
        return {"type": "face_event", "ts": 1}

    def check(self, msg):
        self.checked += 1
        return ["缺少字段 frame_ts"]


class FakeReader(object):
    """假视频源：每次 open 都从源帧 0 开始，读满 length 帧后返回一次 EOF。"""

    def __init__(self, length, fps, open_index):
        self.length = max(0, int(length))
        self.fps = float(fps)
        self.open_index = int(open_index)
        self._index = 0
        self.read_calls = 0
        self.released = False

    def read(self):
        self.read_calls += 1
        if self._index >= self.length:
            return False, None
        self._index += 1
        return True, {"source_frame_index": self._index - 1, "open": self.open_index}

    def meta(self):
        return {"width": 640, "height": 360, "fps_header": self.fps,
                "frame_count_header": self.length, "fourcc": 0}

    def release(self):
        self.released = True

    def pos_msec(self):
        return self._index * 1000.0 / self.fps if self.fps else float("nan")


class DecoderSource(object):
    """reader_factory 替身：按 loop_lengths 给第 N 次 open 决定本圈帧数。"""

    def __init__(self, lengths, fps=12.0):
        self.lengths = [int(value) for value in lengths]
        self.fps = float(fps)
        self.opens = 0
        self.readers = []

    def __call__(self, path):
        length = self.lengths[min(self.opens, len(self.lengths) - 1)]
        self.opens += 1
        reader = FakeReader(length, self.fps, self.opens)
        self.readers.append(reader)
        return reader


# ---------------------------------------------------------------- 运行辅助

def run_case(source, duration_sec=1.0, sample_fps=2.0, detector=None, tracker_obj=None,
             events_module=None, clock=None, monitor=None, on_sample=None):
    """用假依赖跑完整 run_probe，并把写出的 JSON 读回来。"""
    clock = clock or FakeClock()
    if tracker_obj is None:
        tracker_obj = real_tracker.FaceTracker()
    deps = probe.ProbeDeps(
        reader_factory=source,
        detector=detector or FakeDetector(),
        tracker_obj=tracker_obj,
        min_face_px=80,
        events_module=events_module or real_events,
        monotonic=clock.monotonic,
        sleep=clock.sleep,
        resource_monitor=monitor,
        cv2_version=None,
    )
    with scratch_dir() as tmp:
        video_path = os.path.join(tmp, "clip.mp4")
        with open(video_path, "wb") as handle:
            handle.write(FAKE_VIDEO_BYTES)
        json_path = os.path.join(tmp, "stability.json")
        args = probe.parse_args([
            "--video", video_path,
            "--json", json_path,
            "--duration-sec", str(duration_sec),
            "--sample-fps", str(sample_fps),
        ])
        code = probe.run_probe(args, deps, on_sample=on_sample)
        with open(json_path, "r", encoding="utf-8") as handle:
            payload = json.load(handle)
    return {"code": code, "payload": payload, "deps": deps, "clock": clock,
            "source": source, "tracker": tracker_obj, "detector": deps.detector}


def collect_samples(source, duration_sec=2.6, **kwargs):
    observed = []

    def on_sample(item, tick, frame):
        observed.append({"observed": dict(item), "tick": tick})

    result = run_case(source, duration_sec=duration_sec, on_sample=on_sample, **kwargs)
    return result, observed


# ---------------------------------------------------------------- 1. 参数校验

class TestRunParameterValidation(unittest.TestCase):
    def test_require_positive_seconds_rejects_non_positive_and_non_finite(self):
        for bad in (0, 0.0, -1, -0.5, float("nan"), float("inf"), float("-inf"),
                    None, "abc"):
            with self.assertRaises(probe.ProbeError):
                probe.require_positive_seconds(bad, "duration")

    def test_require_positive_seconds_accepts_finite_positive(self):
        self.assertEqual(probe.require_positive_seconds(600, "duration"), 600.0)
        self.assertEqual(probe.require_positive_seconds("0.5", "duration"), 0.5)

    def test_positive_duration_argparse_type_reports_usage_error(self):
        for bad in (0, -1, float("nan"), float("inf")):
            with self.assertRaises(argparse.ArgumentTypeError):
                probe.positive_duration(bad)

    def test_cli_rejects_bad_duration_and_sample_fps(self):
        for flag in ("--duration-sec", "--sample-fps"):
            for bad in ("0", "-2", "nan", "inf"):
                stderr = io.StringIO()
                with contextlib.redirect_stderr(stderr):
                    with self.assertRaises(SystemExit):
                        probe.parse_args(["--video", "v.mp4", "--json", "o.json",
                                          flag, bad])

    def test_cli_defaults_follow_the_task_card(self):
        args = probe.parse_args(["--video", "v.mp4", "--json", "o.json"])
        self.assertEqual(args.duration_sec, 600.0)
        self.assertEqual(args.sample_fps, 2.0)
        self.assertEqual(args.profile, "jetson_local")
        self.assertEqual(args.time_source, "index-fps")

    def test_time_source_must_be_the_explicit_cumulative_clock(self):
        self.assertEqual(probe.check_time_source("index-fps"), "index-fps")
        for bad in ("pts", "wallclock", "", None):
            with self.assertRaises(probe.ProbeError):
                probe.check_time_source(bad)

    def test_video_input_rejects_device_number_directory_and_missing(self):
        for bad in ("0", "1", "", None):
            with self.assertRaises(probe.ProbeError):
                probe.check_video_input(bad)
        with scratch_dir() as tmp:
            with self.assertRaises(probe.ProbeError):
                probe.check_video_input(tmp)
            path = os.path.join(tmp, "clip.mp4")
            open(path, "wb").close()
            self.assertEqual(probe.check_video_input(path), os.path.abspath(path))
        with self.assertRaises(probe.ProbeError):
            probe.check_video_input(os.path.join(tempfile.gettempdir(),
                                                 "missing-video-xyz.mp4"))

    def test_validation_happens_before_opening_the_video(self):
        class ExplodingSource(object):
            def __init__(self):
                self.opens = 0

            def __call__(self, path):
                self.opens += 1
                raise AssertionError("参数错误时不应该打开视频")

        with scratch_dir() as tmp:
            video = os.path.join(tmp, "clip.mp4")
            open(video, "wb").close()
            valid = ["--video", video, "--json", os.path.join(tmp, "o.json")]
            bad_cases = (
                {"duration_sec": 0},
                {"duration_sec": float("nan")},
                {"duration_sec": float("inf")},
                {"sample_fps": 0.0},
                {"sample_fps": float("inf")},
                {"time_source": "pts"},
                {"video": "0"},
            )
            for case in bad_cases:
                source = ExplodingSource()
                args = probe.parse_args(list(valid))
                for key, value in case.items():
                    setattr(args, key, value)
                deps = probe.ProbeDeps(source, FakeDetector(), real_tracker.FaceTracker())
                with self.assertRaises(probe.ProbeError):
                    probe.run_probe(args, deps)
                self.assertEqual(source.opens, 0,
                                 "参数 %r 错误时仍然打开了视频" % (case,))

            missing_parent = probe.parse_args([
                "--video", video,
                "--json", os.path.join(tmp, "no-such-dir", "o.json")])
            source = ExplodingSource()
            with self.assertRaises(probe.ProbeError):
                probe.run_probe(missing_parent,
                                probe.ProbeDeps(source, FakeDetector(),
                                                real_tracker.FaceTracker()))
            self.assertEqual(source.opens, 0)

    def test_source_fps_must_be_finite_and_positive(self):
        for bad in (0.0, -12.0, float("nan"), float("inf")):
            source = DecoderSource([24], fps=bad)
            result = run_case(source, duration_sec=1.0)
            self.assertEqual(result["code"], probe.EXIT_PROBLEM)
            self.assertTrue(result["payload"]["partial"])
            self.assertTrue(result["payload"]["errors"])
            self.assertTrue(any("FPS" in item for item in result["payload"]["errors"]))
            self.assertEqual(source.readers[0].read_calls, 0)


# ---------------------------------------------------------------- 2. 累计循环时钟

class TestCumulativeLoopClock(unittest.TestCase):
    def test_12fps_2hz_grid_hits_frames_0_6_12_and_survives_eof(self):
        source = DecoderSource([24])          # 2 秒 12fps = 24 帧一圈
        result, samples = collect_samples(source, duration_sec=2.6)
        self.assertEqual(result["code"], probe.EXIT_OK)
        self.assertEqual([round(item["observed"]["media_ms"], 3) for item in samples],
                         [0.0, 500.0, 1000.0, 1500.0, 2000.0, 2500.0])
        self.assertEqual([item["observed"]["cumulative_frame_index"] for item in samples],
                         [0, 6, 12, 18, 24, 30])
        self.assertEqual([item["observed"]["source_frame_index"] for item in samples],
                         [0, 6, 12, 18, 0, 6])
        self.assertEqual([item["observed"]["loop_index"] for item in samples],
                         [0, 0, 0, 0, 1, 1])
        for item in samples:
            self.assertEqual(round(item["tick"], 3),
                             round(item["observed"]["media_ms"], 3))

    def test_media_time_never_rewinds_to_source_frame_zero(self):
        source = DecoderSource([24])
        result, samples = collect_samples(source, duration_sec=2.6)
        self.assertEqual(result["code"], probe.EXIT_OK)
        for before, after in zip(samples, samples[1:]):
            self.assertGreater(after["observed"]["media_ms"],
                               before["observed"]["media_ms"])
        crossing = samples[4]["observed"]
        self.assertEqual(crossing["media_ms"], 2000.0)
        self.assertEqual(crossing["source_frame_index"], 0)
        self.assertEqual(crossing["cumulative_frame_index"], 24)
        self.assertEqual(crossing["loop_index"], 1)

    def test_decode_counts_and_decoder_reopen(self):
        source = DecoderSource([24])
        result, _samples = collect_samples(source, duration_sec=2.6)
        decode = result["payload"]["decode"]
        self.assertEqual(decode["decoded_frames_total"], 31)
        self.assertEqual(decode["sampled_frames"], 6)
        self.assertEqual(decode["skipped_frames"], 25)
        self.assertEqual(decode["loops_completed"], 1)
        self.assertEqual(decode["eofs"], 1)
        self.assertEqual(decode["decoder_opens"], 2)
        self.assertEqual(decode["frames_in_first_loop"], 24)
        self.assertEqual(result["payload"]["stop_reason"], "duration")


# ---------------------------------------------------------------- 3. 跟踪器跨循环

class TestTrackerAcrossLoops(unittest.TestCase):
    def test_only_decoder_reopens_and_same_spatial_track_emits_once(self):
        source = DecoderSource([24])
        detector = FakeDetector([(10, 10, 120, 120)])
        tracker_obj = real_tracker.FaceTracker(confirm_frames=3, cooldown_sec=10.0,
                                               track_ttl_sec=2.0)
        result = run_case(source, duration_sec=2.6, detector=detector,
                          tracker_obj=tracker_obj)
        payload = result["payload"]

        self.assertEqual(result["code"], probe.EXIT_OK)
        self.assertEqual(source.opens, 2)                 # 只重开解码器
        self.assertEqual(detector.calls, 6)               # 检测器按采样次数调用
        self.assertIs(result["deps"].detector, detector)
        self.assertIs(result["deps"].tracker_obj, tracker_obj)

        # 真实跟踪器状态跨循环保留：同一条轨道，没有因为重播新建轨道
        self.assertEqual(tracker_obj.track_count, 1)
        self.assertEqual(tracker_obj._next_track_id, 2)
        self.assertEqual(payload["tracker"]["final_track_count"], 1)
        self.assertEqual(payload["tracker"]["final_next_track_id"], 2)
        track = payload["tracker"]["final_tracks"][0]
        self.assertEqual(track["id"], 1)
        self.assertTrue(track["emitted"])

        # 同一未知空间轨道只上报一次，且协议合规
        self.assertEqual(payload["events"]["count"], 1)
        self.assertEqual(payload["events"]["protocol_violations"], 0)
        stored = payload["events"]["stored"]
        self.assertEqual(len(stored), 1)
        msg = stored[0]["face_event"]
        self.assertEqual(msg["frame_ts"], msg["ts"])      # 不把处理墙钟混进协议
        self.assertEqual(msg["frame_ts"], probe.BASE_FRAME_TS_MS + 1000)
        self.assertFalse(msg["is_known"])
        self.assertIsNone(msg["name"])


# ---------------------------------------------------------------- 4. 失败与资源释放

class TestFailureHandling(unittest.TestCase):
    def test_zero_frame_video_fails_with_partial_json(self):
        source = DecoderSource([0])
        result = run_case(source, duration_sec=1.0)
        self.assertEqual(result["code"], probe.EXIT_PROBLEM)
        self.assertTrue(result["payload"]["partial"])
        self.assertTrue(result["payload"]["errors"])
        self.assertTrue(any("0 帧" in item or "一帧" in item
                            for item in result["payload"]["errors"]))
        self.assertTrue(source.readers and source.readers[0].released)

    def test_loop_length_change_fails(self):
        source = DecoderSource([12, 13])
        result = run_case(source, duration_sec=2.6)
        self.assertEqual(result["code"], probe.EXIT_PROBLEM)
        self.assertTrue(result["payload"]["partial"])
        self.assertTrue(any("长度变化" in item for item in result["payload"]["errors"]))
        self.assertTrue(all(reader.released for reader in source.readers))

    def test_duration_stop_releases_decoder_without_partial(self):
        source = DecoderSource([24])
        result = run_case(source, duration_sec=1.0)
        self.assertEqual(result["code"], probe.EXIT_OK)
        self.assertFalse(result["payload"]["partial"])
        self.assertEqual(result["payload"]["errors"], [])
        self.assertEqual(result["payload"]["stop_reason"], "duration")
        self.assertTrue(source.readers[-1].released)

    def test_detector_exception_becomes_partial_error(self):
        source = DecoderSource([24])
        result = run_case(source, duration_sec=1.0, detector=ExplodingDetector())
        self.assertEqual(result["code"], probe.EXIT_PROBLEM)
        self.assertTrue(result["payload"]["partial"])
        self.assertTrue(any("检测异常" in item
                            for item in result["payload"]["errors"]))
        self.assertTrue(source.readers[-1].released)

    def test_mid_run_failure_keeps_real_progress_in_partial_json(self):
        source = DecoderSource([24])
        detector = FlakyDetector(fail_on=3)
        result = run_case(source, duration_sec=2.6, detector=detector)
        payload = result["payload"]
        self.assertEqual(result["code"], probe.EXIT_PROBLEM)
        self.assertTrue(payload["partial"])
        self.assertFalse(payload["ok"])
        self.assertTrue(any("检测异常" in item for item in payload["errors"]))
        # 失败前的真实进度必须留在部分 JSON 里，不能归零
        self.assertEqual(payload["clock"]["frames_observed"], 13)
        self.assertEqual(payload["decode"]["decoded_frames_total"], 13)
        self.assertEqual(payload["decode"]["sampled_frames"], 3)
        self.assertEqual(payload["sampling"]["samples"], 3)
        self.assertEqual(payload["detection"]["samples"], 3)
        self.assertEqual(payload["detection"]["detections_total"], 2)
        self.assertTrue(source.readers[-1].released)


# ---------------------------------------------------------------- 5. 真实节拍与迟到

class TestWallClockPacing(unittest.TestCase):
    def test_pacer_waits_to_exact_monotonic_targets(self):
        clock = FakeClock()
        pacer = probe.MonotonicPacer(2.0, monotonic=clock.monotonic, sleep=clock.sleep)
        ticks = [pacer.wait() for _ in range(3)]
        self.assertEqual(pacer.ticks, 3)
        self.assertEqual([round(item["target_sec"] - pacer.origin_sec, 6)
                          for item in ticks], [0.0, 0.5, 1.0])
        self.assertEqual(clock.sleeps, [0.5, 0.5])
        self.assertEqual(pacer.late_ticks, 0)
        self.assertEqual(round(pacer.report()["sleep_ms_total"], 3), 1000.0)
        self.assertEqual(pacer.histogram.report()["count"], 3)

    def test_lateness_is_counted_when_work_overruns(self):
        clock = FakeClock()
        pacer = probe.MonotonicPacer(2.0, monotonic=clock.monotonic, sleep=clock.sleep)
        pacer.wait()
        clock.advance(0.7)                     # 一帧处理耗时 700ms
        info = pacer.wait()
        self.assertAlmostEqual(info["late_ms"], 200.0, places=6)
        self.assertEqual(pacer.late_ticks, 1)
        self.assertAlmostEqual(pacer.report()["late_ms_total"], 200.0, places=6)
        self.assertGreaterEqual(pacer.report()["late_ms_max"], 200.0)
        self.assertEqual(pacer.histogram.report()["count"], 2)

    def test_pacer_rejects_non_positive_rate(self):
        for bad in (0, -2, float("nan"), float("inf")):
            with self.assertRaises(probe.ProbeError):
                probe.MonotonicPacer(bad)

    def test_run_paces_every_tick_instead_of_one_final_sleep(self):
        source = DecoderSource([24])
        clock = FakeClock()
        result = run_case(source, duration_sec=1.0, clock=clock)
        pacing = result["payload"]["pacing"]
        self.assertEqual(result["code"], probe.EXIT_OK)
        self.assertEqual(pacing["ticks"], 3)
        self.assertEqual(result["payload"]["sampling"]["samples"], 3)
        self.assertEqual(round(pacing["sleep_ms_total"], 3), 1000.0)
        self.assertEqual(len(clock.sleeps), 2)
        # 每次只睡到下一个节拍，不是最后一次性睡满时长
        self.assertLessEqual(max(clock.sleeps), 0.5 + 1e-9)

    def test_slow_detection_is_reported_as_lateness(self):
        source = DecoderSource([24])
        clock = FakeClock()
        detector = FakeDetector(clock=clock, work_sec=0.7)
        result = run_case(source, duration_sec=1.0, clock=clock, detector=detector)
        payload = result["payload"]
        self.assertEqual(result["code"], probe.EXIT_OK)
        self.assertEqual(payload["errors"], [])
        self.assertEqual(payload["pacing"]["late_ticks"], 1)
        self.assertGreater(payload["pacing"]["late_ms_total"], 150.0)
        self.assertGreater(payload["run"]["actual_wall_sec"], 1.0)
        self.assertEqual(payload["run"]["stop_reason"], "duration")

    def test_window_waits_out_the_remaining_duration(self):
        source = DecoderSource([24])
        clock = FakeClock()
        result = run_case(source, duration_sec=1.1, clock=clock)
        payload = result["payload"]
        self.assertEqual(result["code"], probe.EXIT_OK)
        self.assertEqual(payload["pacing"]["ticks"], 3)
        self.assertEqual(payload["sampling"]["samples"], 3)
        self.assertGreaterEqual(payload["run"]["actual_wall_sec"], 1.1 - 1e-6)
        self.assertTrue(payload["run"]["window_complete"])
        self.assertEqual(len(clock.sleeps), 3)
        self.assertAlmostEqual(sum(clock.sleeps), 1.1, places=6)
        self.assertAlmostEqual(clock.sleeps[-1], 0.1, places=6)
        # 末尾只等待剩余时长，不处理窗口外的 1500ms 采样来凑时间
        self.assertEqual(payload["sampling"]["last_sample"]["media_ms"], 1000.0)

    def test_incomplete_wall_window_is_not_ok(self):
        class StingyClock(FakeClock):
            def sleep(self, seconds):
                super(StingyClock, self).sleep(seconds * 0.5)   # 平台提前返回

        source = DecoderSource([24])
        clock = StingyClock()
        result = run_case(source, duration_sec=1.1, clock=clock)
        payload = result["payload"]
        self.assertEqual(result["code"], probe.EXIT_PROBLEM)
        self.assertFalse(payload["partial"])
        self.assertFalse(payload["run"]["window_complete"])
        self.assertFalse(payload["ok"])
        self.assertEqual(payload["errors"], [])
        self.assertEqual(payload["run"]["stop_reason"], "duration")


# ---------------------------------------------------------------- 6. 资源与协议

def write_fake_proc(root, rss_kb=102400, threads=5, fds=3):
    self_dir = os.path.join(root, "self")
    os.makedirs(os.path.join(self_dir, "fd"))
    with open(os.path.join(self_dir, "status"), "w", encoding="utf-8") as handle:
        handle.write("Name:\tpython3\n")
        handle.write("VmRSS:\t %d kB\n" % int(rss_kb))
        handle.write("VmSize:\t 2048 kB\n")
        handle.write("Threads:\t%d\n" % int(threads))
    for index in range(int(fds)):
        open(os.path.join(self_dir, "fd", str(index)), "w").close()


def resource_snapshot(elapsed_sec, rss_mib, fd_count, threads):
    def raw(value, cast):
        if value is None or isinstance(value, str):
            return value
        if isinstance(value, float) and not math.isfinite(value):
            return value
        return cast(value)

    return {"supported": True, "elapsed_sec": float(elapsed_sec),
            "rss_mib": raw(rss_mib, float), "fd_count": raw(fd_count, int),
            "threads": raw(threads, int)}


class TestResourceMonitoring(unittest.TestCase):
    def test_parse_proc_status(self):
        parsed = probe.parse_proc_status(
            "Name:\tpython3\nVmRSS:\t 123456 kB\nVmSize:\t 2048 kB\nThreads:\t7\n")
        self.assertEqual(parsed["rss_kb"], 123456)
        self.assertAlmostEqual(parsed["rss_mib"], 123456 / 1024.0, places=6)
        self.assertEqual(parsed["threads"], 7)

    def test_parse_proc_status_missing_fields_are_none_not_zero(self):
        parsed = probe.parse_proc_status("Name:\tpython3\n")
        self.assertIsNone(parsed["rss_kb"])
        self.assertIsNone(parsed["rss_mib"])
        self.assertIsNone(parsed["threads"])

    def test_read_resources_from_fake_proc(self):
        with scratch_dir() as tmp:
            write_fake_proc(tmp, rss_kb=102400, threads=5, fds=3)
            snapshot = probe.read_resources(tmp)
        self.assertTrue(snapshot["supported"])
        self.assertEqual(snapshot["rss_kb"], 102400)
        self.assertAlmostEqual(snapshot["rss_mib"], 100.0, places=6)
        self.assertEqual(snapshot["threads"], 5)
        self.assertEqual(snapshot["fd_count"], 3)

    def test_non_linux_is_unsupported_not_zero(self):
        with scratch_dir() as tmp:
            snapshot = probe.read_resources(os.path.join(tmp, "no-proc"))
        self.assertFalse(snapshot["supported"])
        self.assertIsNone(snapshot["rss_mib"])
        self.assertIsNone(snapshot["threads"])
        self.assertIsNone(snapshot["fd_count"])
        self.assertIn("unsupported", snapshot["reason"])

    def test_snapshot_capacity_is_bounded(self):
        with scratch_dir() as tmp:
            write_fake_proc(tmp)
            monitor = probe.ResourceMonitor(proc_root=tmp, max_snapshots=4)
            for index in range(20):
                monitor.observe(index * 5.0)
            report = monitor.report()
        self.assertLessEqual(len(monitor.snapshots), 4)
        self.assertGreaterEqual(len(monitor.snapshots), 2)
        self.assertEqual(report["snapshot_count"], len(monitor.snapshots))
        self.assertGreater(report["decimations"], 0)

    def test_verdict_uses_warmup_baseline_and_growth_limits(self):
        ok = probe.resource_verdict(
            [resource_snapshot(0, 100.0, 5, 3),
             resource_snapshot(60, 101.0, 5, 4),
             resource_snapshot(600, 110.0, 6, 5)],
            60.0, 64.0, 2, 4)
        self.assertEqual(ok["status"], "ok")
        self.assertTrue(ok["ok"])
        self.assertAlmostEqual(ok["growth"]["rss_mib"], 9.0, places=6)
        self.assertEqual(ok["growth"]["fd"], 1)
        self.assertEqual(ok["growth"]["threads"], 1)

        bad = probe.resource_verdict(
            [resource_snapshot(0, 100.0, 5, 3),
             resource_snapshot(60, 100.0, 5, 3),
             resource_snapshot(600, 200.0, 8, 9)],
            60.0, 64.0, 2, 4)
        self.assertEqual(bad["status"], "growth_exceeded")
        self.assertFalse(bad["ok"])
        self.assertIn("rss", bad["exceeded"])
        self.assertIn("fd", bad["exceeded"])
        self.assertIn("threads", bad["exceeded"])

        short = probe.resource_verdict(
            [resource_snapshot(0, 100.0, 5, 3),
             resource_snapshot(30, 100.0, 5, 3)],
            60.0, 64.0, 2, 4)
        self.assertEqual(short["status"], "insufficient_warmup")
        self.assertIsNone(short["ok"])

        unsupported = probe.resource_verdict([], 60.0, 64.0, 2, 4)
        self.assertEqual(unsupported["status"], "unsupported")
        self.assertIsNone(unsupported["ok"])

    def test_verdict_is_insufficient_when_required_fields_missing(self):
        cases = (
            # 基线（预热后首个快照）缺字段
            (60.0, [resource_snapshot(0, 100.0, 5, 3),
                    resource_snapshot(60, None, 5, 3)]),
            (0.0, [resource_snapshot(0, 100.0, None, 3),
                   resource_snapshot(60, 101.0, 5, 3)]),
            (0.0, [resource_snapshot(0, 100.0, 5, None),
                   resource_snapshot(60, 101.0, 5, 3)]),
            # 末次快照缺字段
            (60.0, [resource_snapshot(0, 100.0, 5, 3),
                    resource_snapshot(60, 101.0, 5, 4),
                    resource_snapshot(600, 110.0, None, 5)]),
            (60.0, [resource_snapshot(0, 100.0, 5, 3),
                    resource_snapshot(600, 110.0, 6, None)]),
        )
        for warmup_sec, snapshots in cases:
            verdict = probe.resource_verdict(snapshots, warmup_sec, 64.0, 2, 4)
            self.assertEqual(verdict["status"], "insufficient_data")
            self.assertIsNone(verdict["ok"])
            self.assertIsNone(verdict.get("growth"))

    def test_verdict_rejects_non_finite_or_negative_values(self):
        for bad in (float("nan"), float("inf"), float("-inf"), -1.0, -1, "abc"):
            verdict = probe.resource_verdict(
                [resource_snapshot(0, 100.0, 5, 3),
                 resource_snapshot(600, bad, 6, 5)],
                60.0, 64.0, 2, 4)
            self.assertEqual(verdict["status"], "insufficient_data")
            self.assertIsNone(verdict["ok"])
            verdict = probe.resource_verdict(
                [resource_snapshot(0, 100.0, bad, 3),
                 resource_snapshot(600, 110.0, 6, 5)],
                0.0, 64.0, 2, 4)
            self.assertEqual(verdict["status"], "insufficient_data")
            self.assertIsNone(verdict["ok"])

    def test_verdict_accepts_real_zero_values(self):
        verdict = probe.resource_verdict(
            [resource_snapshot(0, 0.0, 0, 0),
             resource_snapshot(60, 0.0, 0, 0),
             resource_snapshot(600, 0.0, 0, 0)],
            60.0, 64.0, 2, 4)
        self.assertEqual(verdict["status"], "ok")
        self.assertTrue(verdict["ok"])
        self.assertEqual(verdict["growth"]["rss_mib"], 0.0)
        self.assertEqual(verdict["growth"]["fd"], 0)
        self.assertEqual(verdict["growth"]["threads"], 0)

    def test_payload_carries_resource_snapshots(self):
        with scratch_dir() as tmp:
            write_fake_proc(tmp)
            monitor = probe.ResourceMonitor(proc_root=tmp, warmup_sec=0.0)
            result = run_case(DecoderSource([24]), duration_sec=1.0, monitor=monitor)
            resources = result["payload"]["resources"]
        self.assertEqual(result["code"], probe.EXIT_OK)
        self.assertTrue(resources["supported"])
        self.assertGreaterEqual(resources["snapshot_count"], 1)
        self.assertEqual(resources["verdict"]["status"], "ok")
        self.assertEqual(resources["snapshots"][0]["elapsed_sec"], 0.0)
        self.assertAlmostEqual(resources["snapshots"][0]["rss_mib"], 100.0, places=6)

    def test_event_protocol_failure_returns_non_zero(self):
        source = DecoderSource([24])
        result = run_case(source, duration_sec=2.6, events_module=BrokenEvents())
        payload = result["payload"]
        self.assertEqual(result["code"], probe.EXIT_PROBLEM)
        self.assertFalse(payload["ok"])
        self.assertEqual(payload["events"]["count"], 1)
        self.assertEqual(payload["events"]["protocol_violations"], 1)
        self.assertEqual(payload["events"]["stored"][0]["problems"],
                         ["缺少字段 frame_ts"])


# ---------------------------------------------------------------- 输出合同

class TestPayloadContract(unittest.TestCase):
    def test_summary_reports_versions_hashes_scope_and_wall_vs_media(self):
        source = DecoderSource([24])
        result = run_case(source, duration_sec=1.0)
        payload = result["payload"]

        self.assertEqual(result["code"], probe.EXIT_OK)
        self.assertEqual(payload["schema"], probe.SCHEMA)
        self.assertEqual(payload["task_card"],
                         "docs/自然场景与稳定性任务卡-2026-11-01.md")

        scope = payload["scope"]
        self.assertEqual(scope["kind"], "offline_artificial_loop_stress")
        self.assertFalse(scope["is_natural_reentry"])
        self.assertFalse(scope["is_field_long_run"])
        self.assertFalse(scope["produces_ground_truth"])
        self.assertTrue(scope["does"])
        self.assertTrue(scope["does_not"])

        self.assertIn("version", payload["interpreter"])
        self.assertIn("version", payload["opencv"])
        self.assertIn("system", payload["platform"])
        self.assertEqual(payload["video"]["sha256"],
                         hashlib.sha256(FAKE_VIDEO_BYTES).hexdigest())
        for relative in ("scripts/video_stability_probe.py",
                         "scripts/video_pipeline_probe.py",
                         "vision/tracker.py", "vision/events.py"):
            self.assertIn(relative, payload["sources"])
            self.assertRegex(payload["sources"][relative], r"^[0-9a-f]{64}$")

        self.assertEqual(payload["params"]["duration_sec"], 1.0)
        self.assertEqual(payload["params"]["sample_fps"], 2.0)
        self.assertEqual(payload["params"]["time_source"], "index-fps")
        self.assertEqual(payload["params"]["profile"], "jetson_local")
        self.assertEqual(payload["clock"]["mode"], "index-fps")

        run = payload["run"]
        self.assertEqual(run["planned_wall_sec"], 1.0)
        self.assertGreaterEqual(run["actual_wall_sec"], 1.0)
        self.assertGreaterEqual(run["media_sec"], 0.0)
        self.assertIn("media_vs_wall", run)
        self.assertEqual(run["stop_reason"], "duration")

        self.assertIn("lateness", payload["pacing"])
        self.assertIn("resources", payload)
        self.assertEqual(payload["warnings"], [])
        self.assertEqual(payload["errors"], [])

    def test_loop_count_is_reported_even_when_no_full_loop_completed(self):
        source = DecoderSource([24])
        result = run_case(source, duration_sec=1.0)
        decode = result["payload"]["decode"]
        self.assertEqual(decode["loops_completed"], 0)
        self.assertEqual(decode["decoder_opens"], 1)
        self.assertEqual(decode["decoded_frames_total"], 13)


# ---------------------------------------------------------------- 兼容性

class TestPython36Compatibility(unittest.TestCase):
    def test_script_parses_with_python36_grammar(self):
        with open(SCRIPT_PATH, "r", encoding="utf-8") as handle:
            source = handle.read()
        if sys.version_info[:2] == (3, 6):
            ast.parse(source, filename=SCRIPT_PATH)
        else:
            try:
                ast.parse(source, filename=SCRIPT_PATH, feature_version=(3, 6))
            except TypeError:
                self.skipTest("当前解释器不支持 feature_version，且不是原生 3.6")
        self.assertNotIn("from __future__ import annotations", source)
        self.assertNotIn(":=", source)

    def test_module_import_does_not_pull_in_cv2_or_yaml(self):
        code = (
            "import importlib.util, sys\n"
            "for path in (%r, %r):\n"
            "    if path not in sys.path:\n"
            "        sys.path.insert(0, path)\n"
            "spec = importlib.util.spec_from_file_location('stability_probe', %r)\n"
            "module = importlib.util.module_from_spec(spec)\n"
            "spec.loader.exec_module(module)\n"
            "bad = [name for name in ('cv2', 'yaml', 'numpy') if name in sys.modules]\n"
            "sys.exit(1 if bad else 0)\n"
        ) % (PROJECT_DIR, SCRIPTS_DIR, SCRIPT_PATH)
        completed = subprocess.run([sys.executable, "-c", code])
        self.assertEqual(completed.returncode, 0,
                         "加载压力探针时导入了 cv2 / yaml / numpy")


if __name__ == "__main__":
    unittest.main()
