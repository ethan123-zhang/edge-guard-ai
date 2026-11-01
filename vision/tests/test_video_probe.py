"""公开视频离线回归探针的纯函数测试（不 import cv2、不读任何视频文件）。

钉住的合同（对应 docs/公开视频回归任务卡-2026-11-01.md）：

- 2Hz 网格抽样：只有落在网格点上的源帧才进检测器/跟踪器，跳过的不调用
- 合法首帧 PTS 0 必须保留（不能被当成"没读到"）
- 无效 / 不前进的 PTS 回退到 frame_index/fps，来源和回退次数都要能看出来
- ``--time-source index-fps`` 是显式 CFR 假设：12fps 下帧 0/6/12 = 0/500/1000 ms，
  不看墙钟、不自作主张减一帧；原始 POS_MSEC 只作诊断
- 0 / 负 / NaN / inf 采样率直接拒绝（CLI 也是）
- 顺序解码到 EOF 就停：EOF 之后不再读一次，绝不循环

这里全部用假 reader + 纯逻辑，所以 PC 没装 OpenCV、板端只有 4.1.1 都能跑。
"""
import argparse
import ast
import contextlib
import importlib.util
import io
import math
import os
import subprocess
import sys
import tempfile
import unittest

PROJECT_DIR = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
SCRIPT_PATH = os.path.join(PROJECT_DIR, "scripts", "video_pipeline_probe.py")
if PROJECT_DIR not in sys.path:
    sys.path.insert(0, PROJECT_DIR)


def _load_probe():
    """按文件路径加载探针脚本：它内部延迟 import cv2，所以这里不需要 OpenCV。"""
    spec = importlib.util.spec_from_file_location("video_pipeline_probe", SCRIPT_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


probe = _load_probe()

FPS_12 = 12.0
CFR_12_PTS = [round(index * 1000.0 / FPS_12, 3) for index in range(40)]


class FakeReader(object):
    """假视频源：按顺序吐出 POS_MSEC。

    ``read_calls`` 和 ``reads_after_eof`` 用来证明"EOF 之后不再读"——真循环播放的
    实现会在 EOF 后再读一次并重置帧号，这里会直接露出来。
    """

    def __init__(self, pts_list):
        self._pts = list(pts_list)
        self.read_calls = 0
        self.reads_after_eof = 0

    def read(self):
        self.read_calls += 1
        if self.read_calls > len(self._pts):
            self.reads_after_eof += 1
            return False, None
        return True, {"frame": self.read_calls - 1}

    def pos_msec(self):
        return self._pts[self.read_calls - 1]


def _collect(sampler, clock, reader):
    samples = []
    stats = probe.decode_and_sample(
        reader, clock, sampler,
        lambda observed, tick, frame: samples.append((observed, tick)),
    )
    return samples, stats


# ---------------------------------------------------------------- 参数校验

class TestSampleFpsValidation(unittest.TestCase):
    def test_accepts_finite_positive(self):
        self.assertEqual(probe.require_positive_fps(2), 2.0)
        self.assertEqual(probe.require_positive_fps("2.5"), 2.5)
        self.assertEqual(probe.require_positive_fps(0.5), 0.5)

    def test_rejects_zero_negative_nan_inf(self):
        for bad in (0, 0.0, -2, -0.5, float("nan"), float("inf"), float("-inf"),
                    None, "abc"):
            with self.assertRaises(probe.ProbeError):
                probe.require_positive_fps(bad)

    def test_cli_type_reports_usage_error(self):
        for bad in (0, -1, float("nan"), float("inf")):
            with self.assertRaises(argparse.ArgumentTypeError):
                probe.positive_fps(bad)

    def test_cli_rejects_bad_sample_fps(self):
        for bad in ("0", "-2", "nan", "inf"):
            stderr = io.StringIO()
            with contextlib.redirect_stderr(stderr):
                with self.assertRaises(SystemExit):
                    probe.parse_args(["--video", "v.mp4", "--json", "o.json",
                                      "--sample-fps", bad])

    def test_cli_defaults_follow_the_task_card(self):
        args = probe.parse_args(["--video", "v.mp4", "--json", "o.json"])
        self.assertEqual(args.sample_fps, 2.0)
        self.assertEqual(args.start, 0.0)
        self.assertIsNone(args.end)
        self.assertEqual(args.profile, "jetson_local")
        self.assertEqual(args.time_source, "pts")


class TestWindowValidation(unittest.TestCase):
    def test_accepts_open_and_valid_windows(self):
        probe.check_window(0.0, None)
        probe.check_window(0.0, 1.0)

    def test_rejects_end_not_greater_than_start(self):
        for start, end in ((1.0, 1.0), (2.0, 1.0)):
            with self.assertRaises(probe.ProbeError):
                probe.check_window(start, end)

    def test_seconds_type_rejects_negative_and_nan(self):
        for bad in (-0.1, float("nan"), "x"):
            with self.assertRaises(argparse.ArgumentTypeError):
                probe.seconds(bad)

    def test_output_parent_is_checked_before_opening_video(self):
        missing = os.path.join(tempfile.gettempdir(), "no-such-dir-xyz", "o.json")
        with self.assertRaises(probe.ProbeError):
            probe.check_json_output(missing)
        with tempfile.TemporaryDirectory() as tmp:
            target = probe.check_json_output(os.path.join(tmp, "o.json"))
            self.assertEqual(os.path.dirname(target), os.path.abspath(tmp))


# ---------------------------------------------------------------- 网格抽样

class TestGridSampling(unittest.TestCase):
    def test_first_tick_matches_media_time(self):
        sampler = probe.GridSampler(2.0)
        self.assertEqual(sampler.next_tick_ms, 0.0)
        self.assertIsNone(sampler.due(-1.0))
        self.assertEqual(sampler.due(0.0), 0.0)

    def test_letterbox_between_ticks_is_not_due(self):
        sampler = probe.GridSampler(2.0)
        sampler.consume(0.0)
        self.assertIsNone(sampler.due(499.9))
        self.assertEqual(sampler.due(500.0), 500.0)
        sampler.consume(500.0)
        self.assertIsNone(sampler.due(999.9))

    def test_two_hz_on_twelve_fps_video_picks_frames_0_6_12(self):
        """12fps 源帧 × 2Hz 网格：帧 0/6/12 命中，其余 10 帧被跳过。"""
        reader = FakeReader(CFR_12_PTS[:13])
        clock = probe.MediaClock(FPS_12, "index-fps")
        sampler = probe.GridSampler(2.0)
        samples, stats = _collect(sampler, clock, reader)
        self.assertEqual([item[0]["frame_index"] for item in samples], [0, 6, 12])
        self.assertEqual([item[1] for item in samples], [0.0, 500.0, 1000.0])
        self.assertEqual(stats["sampled_frames"], 3)
        self.assertEqual(stats["skipped_frames"], 10)
        self.assertEqual(sampler.ticks_skipped, 0)

    def test_start_offset_uses_the_global_grid(self):
        sampler = probe.GridSampler(2.0, start_s=30.1)
        self.assertEqual(sampler.next_tick_ms, 30500.0)
        self.assertIsNone(sampler.due(30499.0))
        self.assertEqual(sampler.due(30500.0), 30500.0)

    def test_end_boundary_stops_at_the_window(self):
        sampler = probe.GridSampler(2.0, end_s=1.0)
        self.assertEqual(sampler.due(0.0), 0.0)
        sampler.consume(0.0)
        self.assertEqual(sampler.due(500.0), 500.0)
        sampler.consume(500.0)
        self.assertIsNone(sampler.due(1000.0))
        self.assertEqual(sampler.sampled, 2)

    def test_one_sample_per_frame_and_skipped_ticks_are_counted(self):
        sampler = probe.GridSampler(2.0)
        sampler.consume(0.0)
        self.assertEqual(sampler.due(1250.0), 500.0)
        sampler.consume(1250.0)
        self.assertEqual(sampler.ticks_skipped, 1)   # 1000ms 这个网格点没有帧可用
        self.assertEqual(sampler.next_tick_ms, 1500.0)


# ---------------------------------------------------------------- 媒体时间

class TestMediaClockPts(unittest.TestCase):
    def test_legal_first_frame_zero_pts_is_kept(self):
        clock = probe.MediaClock(FPS_12, "pts")
        observed = clock.observe(0, 0.0)
        self.assertEqual(observed["media_ms"], 0.0)
        self.assertEqual(observed["source"], probe.SRC_PTS)
        self.assertEqual(observed["pts_status"], "ok")
        self.assertEqual(clock.first_media_ms, 0.0)

    def test_valid_pts_sequence_stays_on_pts(self):
        clock = probe.MediaClock(FPS_12, "pts")
        media = [clock.observe(index, pts)["media_ms"]
                 for index, pts in enumerate(CFR_12_PTS[:5])]
        self.assertEqual(media, CFR_12_PTS[:5])
        self.assertEqual(clock.pts_used, 5)
        self.assertEqual(clock.fallback, 0)
        self.assertEqual(clock.report()["monotonic_violations"], 0)

    def test_invalid_pts_falls_back_to_index_over_fps(self):
        for bad in (None, float("nan"), float("inf"), -5.0, "oops"):
            clock = probe.MediaClock(FPS_12, "pts")
            clock.observe(0, 0.0)
            observed = clock.observe(3, bad)
            self.assertEqual(observed["source"], probe.SRC_INDEX_FALLBACK)
            self.assertEqual(observed["media_ms"], 250.0)
            self.assertEqual(observed["pts_status"], "invalid")
            self.assertEqual(clock.fallback, 1)
            self.assertEqual(clock.pts_invalid, 1)

    def test_backwards_pts_falls_back_instead_of_rewinding_time(self):
        clock = probe.MediaClock(FPS_12, "pts")
        clock.observe(0, 0.0)
        clock.observe(1, 83.333)
        observed = clock.observe(2, 41.0)             # PTS 倒退
        self.assertEqual(observed["source"], probe.SRC_INDEX_FALLBACK)
        self.assertGreater(observed["media_ms"], 83.333)
        self.assertEqual(clock.fallback, 1)

    def test_repeated_pts_falls_back_when_index_time_advances(self):
        clock = probe.MediaClock(FPS_12, "pts")
        clock.observe(0, 0.0)
        observed = clock.observe(1, 0.0)              # PTS 原地不动
        self.assertEqual(observed["source"], probe.SRC_INDEX_FALLBACK)
        self.assertGreater(observed["media_ms"], 0.0)

    def test_fallback_that_cannot_advance_fails_instead_of_faking_time(self):
        """回退值比上一帧还小：必须失败，不能钳成 last+1ms 伪造单调时间。"""
        clock = probe.MediaClock(FPS_12, "pts")
        clock.observe(0, 2000.0)                      # 回退基准 83.33 ms << 2000 ms
        with self.assertRaises(probe.ProbeError):
            clock.observe(1, None)
        self.assertEqual(clock.violations, 1)
        self.assertEqual(clock.last_ms, 2000.0)       # 失败不污染已确认的时间
        self.assertEqual(clock.frames, 1)

    def test_invalid_fps_with_unusable_pts_fails(self):
        clock = probe.MediaClock(0.0, "pts")
        with self.assertRaises(probe.ProbeError):
            clock.observe(0, None)

    def test_invalid_fps_is_fine_while_pts_are_good(self):
        clock = probe.MediaClock(float("nan"), "pts")
        self.assertEqual(clock.observe(0, 0.0)["media_ms"], 0.0)
        self.assertEqual(clock.observe(1, 83.333)["media_ms"], 83.333)


class TestMediaClockIndexFps(unittest.TestCase):
    def test_twelve_fps_contract_frames_0_6_12(self):
        clock = probe.MediaClock(FPS_12, "index-fps")
        frames = [clock.observe(index, pts)["media_ms"]
                  for index, pts in enumerate(CFR_12_PTS[:13])]
        self.assertEqual(frames[0], 0.0)
        self.assertEqual(frames[6], 500.0)
        self.assertEqual(frames[12], 1000.0)

    def test_sources_are_labelled_explicit_cfr(self):
        clock = probe.MediaClock(FPS_12, "index-fps")
        for index in range(3):
            self.assertEqual(clock.observe(index, CFR_12_PTS[index])["source"],
                             probe.SRC_INDEX_CFR)
        self.assertEqual(clock.report()["contract"], probe.SRC_INDEX_CFR)

    def test_pos_msec_is_diagnostic_only(self):
        """板端 4.1.1 回报的是"下一帧时刻"：index-fps 下不能影响媒体时间。"""
        clock = probe.MediaClock(FPS_12, "index-fps")
        first = clock.observe(0, 83.333)
        second = clock.observe(1, 166.667)
        self.assertEqual(first["media_ms"], 0.0)
        self.assertEqual(second["media_ms"], 1000.0 / FPS_12)
        self.assertEqual(first["pts_ms"], 83.333)
        self.assertEqual(second["pts_ms"], 166.667)

    def test_invalid_fps_cannot_build_index_clock(self):
        with self.assertRaises(probe.ProbeError):
            probe.MediaClock(0.0, "index-fps")
        with self.assertRaises(probe.ProbeError):
            probe.MediaClock(float("nan"), "index-fps")

    def test_unknown_mode_is_rejected(self):
        with self.assertRaises(probe.ProbeError):
            probe.MediaClock(FPS_12, "wallclock")

    def test_frame_ts_is_base_plus_media(self):
        self.assertEqual(probe.frame_ts_from_media(0.0), probe.BASE_FRAME_TS_MS)
        self.assertEqual(probe.frame_ts_from_media(500.0),
                         probe.BASE_FRAME_TS_MS + 500)
        self.assertEqual(probe.frame_ts_from_media(1000.0 / FPS_12 * 12),
                         probe.BASE_FRAME_TS_MS + 1000)
        self.assertEqual(probe.frame_ts_from_media(83.333),
                         probe.BASE_FRAME_TS_MS + 83)


# ---------------------------------------------------------------- 解码循环

class TestDecodeLoop(unittest.TestCase):
    def test_stops_at_eof_without_looping(self):
        reader = FakeReader(CFR_12_PTS[:13])
        clock = probe.MediaClock(FPS_12, "index-fps")
        _samples, stats = _collect(probe.GridSampler(2.0), clock, reader)
        self.assertTrue(stats["eof"])
        self.assertEqual(stats["stop_reason"], "eof")
        self.assertEqual(stats["decoded_frames"], 13)
        self.assertEqual(reader.read_calls, 14)      # 13 帧 + 1 次 EOF 确认
        self.assertEqual(reader.reads_after_eof, 1)  # 没有第二次读

    def test_skipped_frames_do_not_reach_the_callback(self):
        reader = FakeReader(CFR_12_PTS[:13])
        clock = probe.MediaClock(FPS_12, "index-fps")
        samples, stats = _collect(probe.GridSampler(2.0), clock, reader)
        frames = [item[0]["frame_index"] for item in samples]
        self.assertEqual(frames, [0, 6, 12])
        self.assertEqual(stats["skipped_frames"], stats["decoded_frames"] - len(frames))

    def test_end_window_truncates_before_sampling(self):
        reader = FakeReader(CFR_12_PTS[:13])
        clock = probe.MediaClock(FPS_12, "index-fps")
        sampler = probe.GridSampler(2.0, end_s=1.0)
        samples, stats = _collect(sampler, clock, reader)
        self.assertEqual(stats["stop_reason"], "end")
        self.assertFalse(stats["eof"])
        self.assertEqual([item[0]["frame_index"] for item in samples], [0, 6])
        self.assertEqual(stats["decoded_frames"], 13)   # 截断帧也真的解码过

    def test_media_time_is_monotonic_even_with_junk_pts(self):
        pts = [0.0, None, 166.667, float("nan"), 0.0, 400.0, None, 40.0]
        clock = probe.MediaClock(FPS_12, "pts")
        media = [clock.observe(index, value)["media_ms"]
                 for index, value in enumerate(pts)]
        for previous, current in zip(media, media[1:]):
            self.assertGreater(current, previous)
        self.assertGreater(clock.fallback, 0)

    def test_unsafe_fallback_stops_the_loop_with_probe_error(self):
        """时间不能安全回退时整轮失败（调用方保留 partial），不带着假时间继续采样。"""
        reader = FakeReader([0.0, 2000.0, None])
        clock = probe.MediaClock(FPS_12, "pts")
        with self.assertRaises(probe.ProbeError):
            _collect(probe.GridSampler(2.0), clock, reader)
        self.assertEqual(clock.violations, 1)

    def test_bad_pts_does_not_break_eof(self):
        reader = FakeReader([0.0, None, None, 300.0])
        clock = probe.MediaClock(FPS_12, "pts")
        sampler = probe.GridSampler(2.0)
        _samples, stats = _collect(sampler, clock, reader)
        self.assertEqual(stats["stop_reason"], "eof")
        self.assertGreaterEqual(sampler.sampled, 1)


# ---------------------------------------------------------------- 兼容性

class TestScriptPy36Compatibility(unittest.TestCase):
    def test_script_parses_with_python36_grammar(self):
        with open(SCRIPT_PATH, "r", encoding="utf-8") as handle:
            source = handle.read()
        if sys.version_info[:2] == (3, 6):
            ast.parse(source, filename=SCRIPT_PATH)      # 原生 3.6 不支持 feature_version
        else:
            try:
                ast.parse(source, filename=SCRIPT_PATH, feature_version=(3, 6))
            except TypeError:
                self.skipTest("当前解释器不支持 feature_version，且不是原生 3.6")
        self.assertNotIn("from __future__ import annotations", source)
        self.assertNotIn(":=", source)

    def test_module_import_does_not_pull_in_cv2(self):
        """独立子进程里加载脚本，确认 cv2 是延迟导入的（纯函数测试不依赖 OpenCV）。"""
        code = (
            "import importlib.util, sys\n"
            "spec = importlib.util.spec_from_file_location('probe', %r)\n"
            "module = importlib.util.module_from_spec(spec)\n"
            "spec.loader.exec_module(module)\n"
            "sys.exit(1 if 'cv2' in sys.modules else 0)\n" % SCRIPT_PATH
        )
        completed = subprocess.run([sys.executable, "-c", code])
        self.assertEqual(completed.returncode, 0, "加载探针脚本时导入了 cv2")


if __name__ == "__main__":
    unittest.main()
