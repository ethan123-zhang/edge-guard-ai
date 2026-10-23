"""配置加载测试：profile 合并、校验报错要**点名具体键**。"""
import unittest

from vision import config


class TestLoad(unittest.TestCase):
    def test_sim_profile_overrides_base(self):
        cfg = config.load("sim")
        self.assertEqual(cfg["profile"], "sim")
        self.assertEqual(cfg["camera"]["backend"], "dshow")
        self.assertTrue(cfg["camera"]["mirror"])
        self.assertEqual(cfg["server"]["ws_url"], "ws://127.0.0.1:8765")

    def test_base_values_survive_the_merge(self):
        cfg = config.load("sim")
        # sim.yaml 没写 detection，应该原样继承 base
        self.assertEqual(cfg["detection"]["score_threshold"], 0.6)
        self.assertEqual(cfg["recognition"]["match_threshold"], 0.363)

    def test_board_profile_is_headless(self):
        cfg = config.load("board")
        self.assertTrue(cfg["runtime"]["headless"])
        self.assertFalse(cfg["camera"]["mirror"])
        self.assertNotEqual(cfg["server"]["ws_url"], cfg["server"]["ws_url"] + "x")

    def test_loading_does_not_mutate_defaults(self):
        config.load("sim")
        self.assertEqual(config.DEFAULTS["camera"]["backend"], "auto")
        self.assertEqual(config.DEFAULTS["runtime"]["max_fps"], 0)

    def test_unknown_profile_names_the_alternatives(self):
        with self.assertRaises(config.ConfigError) as ctx:
            config.load("does-not-exist")
        self.assertIn("sim", str(ctx.exception))


class TestValidate(unittest.TestCase):
    def test_rejects_out_of_range_threshold(self):
        cfg = config.load("sim")
        cfg["detection"]["score_threshold"] = 1.5
        with self.assertRaises(config.ConfigError) as ctx:
            config.validate(cfg)
        self.assertIn("score_threshold", str(ctx.exception))

    def test_rejects_bad_backend(self):
        cfg = config.load("sim")
        cfg["recognition"]["backend"] = "magic"
        with self.assertRaises(config.ConfigError) as ctx:
            config.validate(cfg)
        self.assertIn("recognition.backend", str(ctx.exception))

    def test_rejects_roi_outside_frame(self):
        cfg = config.load("sim")
        cfg["roi"]["enabled"] = True
        cfg["roi"]["x"] = 0.8
        cfg["roi"]["w"] = 0.4
        with self.assertRaises(config.ConfigError):
            config.validate(cfg)

    def test_rejects_confirm_frames_zero(self):
        cfg = config.load("sim")
        cfg["recognition"]["confirm_frames"] = 0
        with self.assertRaises(config.ConfigError) as ctx:
            config.validate(cfg)
        self.assertIn("confirm_frames", str(ctx.exception))


class TestPaths(unittest.TestCase):
    def test_models_and_gallery_live_under_vision(self):
        path = config.resolve_vision("models/x.onnx").replace("\\", "/")
        self.assertTrue(path.endswith("vision/models/x.onnx"), path)

    def test_snapshots_live_under_project_root(self):
        path = config.resolve_project("snapshots").replace("\\", "/")
        self.assertTrue(path.endswith("/snapshots"), path)
        self.assertNotIn("/vision/snapshots", path)


if __name__ == "__main__":
    unittest.main()
