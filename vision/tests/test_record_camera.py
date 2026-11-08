"""素材录制脚本（`scripts/record_camera.py`）的纯函数部分。

交互路径（窗口 + 按键）没法在单测里点，所以这里只测"录错文件/时间算错"这类
会静默毁素材的逻辑：文件名防覆盖、时长格式、帧率估计与写盘帧率取舍、回读校验。
真实录制走 `py -3.12 scripts/record_camera.py --headless --seconds N`（端到端自检）。
"""
import importlib.util
import os
import re
import tempfile
import unittest
from types import SimpleNamespace

import cv2
import numpy as np

PROJECT_DIR = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
SCRIPT = os.path.join(PROJECT_DIR, "scripts", "record_camera.py")


def load_script():
    spec = importlib.util.spec_from_file_location("record_camera_under_test", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class RecorderHelpersTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.rec = load_script()

    def test_unique_path_does_not_overwrite(self):
        with tempfile.TemporaryDirectory() as tmp:
            first = self.rec.unique_path(tmp, "sc01_replacement", "mp4")
            self.assertEqual(os.path.basename(first), "sc01_replacement.mp4")
            open(first, "wb").close()
            second = self.rec.unique_path(tmp, "sc01_replacement", "mp4")
            self.assertEqual(os.path.basename(second), "sc01_replacement_2.mp4")
            open(second, "wb").close()
            third = self.rec.unique_path(tmp, "sc01_replacement", "mp4")
            self.assertEqual(os.path.basename(third), "sc01_replacement_3.mp4")

    def test_unique_path_returns_free_path(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = self.rec.unique_path(tmp, "rec_20261108_100000", "mp4")
            self.assertFalse(os.path.exists(path))
            self.assertEqual(path, os.path.join(tmp, "rec_20261108_100000.mp4"))

    def test_format_hms(self):
        self.assertEqual(self.rec.format_hms(0), "00:00")
        self.assertEqual(self.rec.format_hms(9.9), "00:09")
        self.assertEqual(self.rec.format_hms(75.4), "01:15")
        self.assertEqual(self.rec.format_hms(3600), "60:00")
        self.assertEqual(self.rec.format_hms(-3), "00:00")

    def test_default_stem_matches_timestamp_pattern(self):
        self.assertRegex(self.rec.default_stem(), r"^rec_\d{8}_\d{6}$")

    def test_estimate_fps_from_even_timestamps(self):
        stamps = [i / 30.0 for i in range(30)]
        self.assertAlmostEqual(self.rec.estimate_fps(stamps), 30.0, places=6)

    def test_estimate_fps_needs_at_least_three_frames(self):
        self.assertIsNone(self.rec.estimate_fps([1.0, 1.033]))
        self.assertIsNone(self.rec.estimate_fps([1.0, 1.0, 1.0]))
        self.assertIsNone(self.rec.estimate_fps([]))

    def test_estimate_fps_uses_latest_window_only(self):
        slow = [i * 0.2 for i in range(5)]                    # 前段 5 fps
        fast = [slow[-1] + i / 30.0 for i in range(1, 46)]    # 后段 30 fps
        self.assertAlmostEqual(self.rec.estimate_fps(slow + fast), 30.0, delta=1.0)

    def test_sane_write_fps_keeps_requested_when_close(self):
        self.assertEqual(self.rec.sane_write_fps(25.5, 30.0), 30.0)
        self.assertEqual(self.rec.sane_write_fps(29.0, 30.0), 30.0)

    def test_sane_write_fps_follows_slow_camera(self):
        # DSHOW 实测 13.6 fps：必须按 13.6 写，否则回放整体加速一倍
        self.assertEqual(self.rec.sane_write_fps(13.6, 30.0), 13.6)
        self.assertEqual(self.rec.sane_write_fps(10.0, 30.0), 10.0)

    def test_sane_write_fps_falls_back_when_unusable(self):
        self.assertEqual(self.rec.sane_write_fps(None, 30.0), 30.0)
        self.assertEqual(self.rec.sane_write_fps(2.0, 30.0), 30.0)

    def test_verify_recording_reports_missing_file(self):
        ok, info = self.rec.verify_recording(os.path.join(tempfile.gettempdir(),
                                                         "definitely_missing_%d.mp4" % os.getpid()))
        self.assertFalse(ok)
        self.assertIn("文件不存在", info["problem"])

    def test_verify_recording_reports_empty_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "empty.mp4")
            open(path, "wb").close()
            ok, info = self.rec.verify_recording(path)
            self.assertFalse(ok)
            self.assertIn("0 字节", info["problem"])

    def test_face_summary_empty(self):
        self.assertEqual(self.rec.face_summary([]), (0, 0))
        self.assertEqual(self.rec.face_summary(None), (0, 0))

    def test_face_summary_counts_and_widest(self):
        faces = [SimpleNamespace(size=120), SimpleNamespace(size=95)]
        self.assertEqual(self.rec.face_summary(faces), (2, 120))
        self.assertEqual(self.rec.face_summary([SimpleNamespace(size=63)]), (1, 63))

    def test_timing_ratio_and_retime_trigger(self):
        # 30 fps 写盘、实际 20.7 fps：回放快 45%，必须重写
        self.assertAlmostEqual(self.rec.timing_ratio(1104, 53.4, 30.0), 0.689, places=2)
        self.assertTrue(self.rec.needs_retime(1104, 53.4, 30.0))
        # 实测与写盘一致：不动
        self.assertFalse(self.rec.needs_retime(600, 20.0, 30.0))
        # 数据不全时不做判断（避免把正常文件改坏）
        self.assertIsNone(self.rec.timing_ratio(0, 20.0, 30.0))
        self.assertFalse(self.rec.needs_retime(0, 20.0, 30.0))

    def test_detect_scaled_maps_boxes_back_to_full_frame(self):
        from vision.detector import FaceDetection

        class FakeDetector(object):
            def __init__(self):
                self.seen = None

            def detect(self, frame):
                self.seen = frame.shape[:2]
                return [FaceDetection(10, 20, 30, 40, 0.9)]

        frame = np.zeros((1080, 1920, 3), np.uint8)
        fake = FakeDetector()
        faces = self.rec.detect_scaled(fake, frame, 640)
        self.assertEqual(fake.seen, (360, 640))       # 先缩到 640 宽再检测
        self.assertAlmostEqual(faces[0].x, 30.0, places=1)      # 10 × 3
        self.assertAlmostEqual(faces[0].y, 60.0, places=1)      # 20 × 3
        self.assertAlmostEqual(faces[0].w, 90.0, places=1)      # 30 × 3
        self.assertEqual(faces[0].score, 0.9)

    def test_detect_scaled_skips_resize_when_already_small(self):
        from vision.detector import FaceDetection

        class FakeDetector(object):
            def __init__(self):
                self.seen = None

            def detect(self, frame):
                self.seen = frame.shape[:2]
                return [FaceDetection(5, 5, 20, 20, 0.8)]

        frame = np.zeros((360, 640, 3), np.uint8)
        fake = FakeDetector()
        faces = self.rec.detect_scaled(fake, frame, 640)
        self.assertEqual(fake.seen, (360, 640))       # 没缩放
        self.assertEqual(faces[0].x, 5)

    def test_retime_video_rewrites_container_fps(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "take.mp4")
            writer = cv2.VideoWriter(path, cv2.VideoWriter_fourcc(*"mp4v"),
                                     30.0, (64, 48))
            if not writer.isOpened():
                self.skipTest("本机没有 mp4v 编码器")
            for index in range(20):
                writer.write(np.full((48, 64, 3), index * 10, np.uint8))
            writer.release()
            ok, frames = self.rec.retime_video(path, 10.0)
            self.assertTrue(ok)
            self.assertEqual(frames, 20)
            cap = cv2.VideoCapture(path)
            try:
                self.assertAlmostEqual(cap.get(cv2.CAP_PROP_FPS), 10.0, places=1)
                read = 0
                while cap.read()[0]:
                    read += 1
            finally:
                cap.release()
            self.assertEqual(read, 20)
            self.assertTrue(os.path.exists(os.path.join(tmp, "take.pre-retime.mp4")))


if __name__ == "__main__":
    unittest.main()