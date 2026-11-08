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


if __name__ == "__main__":
    unittest.main()