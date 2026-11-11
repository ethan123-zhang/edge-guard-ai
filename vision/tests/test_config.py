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


class TestHaarEqualizeFlag(unittest.TestCase):
    """detection.haar_equalize：默认 True；只允许严格 bool；jetson_local 显式关闭。"""

    def test_default_is_true(self):
        self.assertIs(config.DEFAULTS["detection"]["haar_equalize"], True)
        self.assertIs(config.load("sim")["detection"]["haar_equalize"], True)

    def test_jetson_local_disables_equalization(self):
        cfg = config.load("jetson_local")
        self.assertIs(cfg["detection"]["haar_equalize"], False)

    def test_missing_key_still_validates(self):
        cfg = config.load("sim")
        del cfg["detection"]["haar_equalize"]
        config.validate(cfg)  # 旧配置缺新键不报错，行为默认 True

    def test_rejects_non_bool_values(self):
        for bad in ("false", "true", 0, 1, None):
            cfg = config.load("sim")
            cfg["detection"]["haar_equalize"] = bad
            with self.assertRaises(config.ConfigError) as ctx:
                config.validate(cfg)
            self.assertIn("detection.haar_equalize", str(ctx.exception))


class TestRuntimeBackend(unittest.TestCase):
    """runtime.backend（2026-11-07 P1）：默认 legacy 不动；显式选择时要能点名报错。"""

    def test_default_is_legacy(self):
        self.assertEqual(config.DEFAULTS["runtime"]["backend"], "legacy")
        self.assertEqual(config.load("sim")["runtime"]["backend"], "legacy")

    def test_accepts_the_four_modes(self):
        for mode in ("legacy", "auto", "cpu", "trt"):
            cfg = config.load("sim")
            cfg["runtime"]["backend"] = mode
            config.validate(cfg)

    def test_defaults_stay_valid_without_engines(self):
        cfg = config.load("sim")
        self.assertEqual(cfg["runtime"]["detector_engine"], "")
        self.assertEqual(cfg["runtime"]["recognizer_engine"], "")
        self.assertIs(cfg["runtime"]["warmup"], True)
        config.validate(cfg)

    def test_rejects_unknown_backend(self):
        cfg = config.load("sim")
        cfg["runtime"]["backend"] = "magic"
        with self.assertRaises(config.ConfigError) as ctx:
            config.validate(cfg)
        self.assertIn("runtime.backend", str(ctx.exception))

    def test_onnx_mode_needs_a_detector_model(self):
        cfg = config.load("sim")
        cfg["runtime"]["backend"] = "cpu"
        cfg["runtime"]["detector_onnx"] = "  "
        with self.assertRaises(config.ConfigError) as ctx:
            config.validate(cfg)
        self.assertIn("detector_onnx", str(ctx.exception))

    def test_recognizer_batch_cannot_exceed_engine_profile(self):
        cfg = config.load("sim")
        cfg["runtime"]["backend"] = "cpu"
        cfg["runtime"]["recognizer_batch"] = 9
        with self.assertRaises(config.ConfigError) as ctx:
            config.validate(cfg)
        self.assertIn("recognizer_batch", str(ctx.exception))

    def test_warmup_must_be_a_real_bool(self):
        cfg = config.load("sim")
        cfg["runtime"]["backend"] = "cpu"
        cfg["runtime"]["warmup"] = "true"
        with self.assertRaises(config.ConfigError) as ctx:
            config.validate(cfg)
        self.assertIn("runtime.warmup", str(ctx.exception))


class TestBoardProfileP1(unittest.TestCase):
    """board.yaml 换成裸 ONNX 管线（2026-11-07 P1）：路径必须是板上的绝对路径。"""

    def test_board_selects_auto_backend(self):
        cfg = config.load("board")
        self.assertEqual(cfg["runtime"]["backend"], "auto")
        self.assertTrue(cfg["runtime"]["detector_engine"].startswith("/home/jetson/"))
        self.assertTrue(cfg["runtime"]["recognizer_engine"].startswith("/home/jetson/"))
        self.assertTrue(cfg["runtime"]["detector_onnx"].startswith("/home/jetson/"))

    def test_board_uses_the_dynamic_sface_the_engine_was_built_from(self):
        cfg = config.load("board")
        self.assertIn("sface_dynamic_batch", cfg["recognition"]["model"])

    def test_board_profile_validates(self):
        config.validate(config.load("board"))

    def test_sim_profile_is_untouched(self):
        cfg = config.load("sim")
        self.assertEqual(cfg["runtime"]["backend"], "legacy")
        self.assertEqual(cfg["runtime"]["detector_engine"], "")


class TestAssociationProfile(unittest.TestCase):
    """关联层默认关、板上开（2026-11-07 P2.5）；质量门按板上实测重标定过。"""

    def test_defaults_are_off(self):
        cfg = config.DEFAULTS["association"]
        self.assertFalse(cfg["enabled"])
        self.assertAlmostEqual(cfg["tau"], 0.45)
        self.assertAlmostEqual(cfg["margin"], 0.10)

    def test_sim_profile_keeps_it_off(self):
        self.assertFalse(config.load("sim")["association"]["enabled"])

    def test_board_profile_enables_it_with_the_recalibrated_gate(self):
        assoc = config.load("board")["association"]
        self.assertTrue(assoc["enabled"], "板上这次就是要接关联层")
        self.assertGreater(assoc["quality_max_rms"], 0.10,
                           "320 输入把关键点量化了，照抄 0.10 只入册 2.5%")
        # 2026-11-11：0.16 在侧脸/遮挡下入册不够，关联退化 track_bound → 同一人重复建会话；
        # 放宽到 0.20 后再用静坐素材复测。
        self.assertAlmostEqual(assoc["quality_max_rms"], 0.20)

    def test_board_profile_widens_track_ttl(self):
        """轨道 TTL 放宽到 5 秒：抬手/转头丢脸 2~5 秒不再拆成新访客。"""
        cfg = config.load("board")
        self.assertEqual(cfg["recognition"]["track_ttl_sec"], 5.0)
        self.assertEqual(cfg["association"]["track_ttl_sec"], 5.0)

    def test_validate_rejects_bad_association_values(self):
        for patch, needle in (
            ({"tau": 1.5}, "association.tau"),
            ({"margin": -0.1}, "association.margin"),
            ({"quality_max_rms": -0.1}, "association.quality_max_rms"),
            ({"enabled": "yes"}, "association.enabled"),
        ):
            cfg = config.load("sim")
            cfg["association"] = dict(config.DEFAULTS["association"])
            cfg["association"].update(patch)
            with self.assertRaises(config.ConfigError) as ctx:
                config.validate(cfg)
            self.assertIn(needle, str(ctx.exception))


class TestCameraFormat(unittest.TestCase):
    """camera.fourcc / camera.fps（2026-11-07 P2）：板端不显式要 MJPG 只有 9.9 fps。"""

    def test_defaults_do_not_touch_negotiation(self):
        cam = config.DEFAULTS["camera"]
        self.assertEqual(cam["fourcc"], "")
        self.assertEqual(int(cam["fps"]), 0)
        self.assertEqual(config.load("sim")["camera"]["fourcc"], "")

    def test_board_requests_mjpg_at_30fps(self):
        cam = config.load("board")["camera"]
        self.assertEqual(cam["fourcc"], "MJPG")
        self.assertEqual(int(cam["fps"]), 30)

    def test_rejects_malformed_fourcc(self):
        for bad in ("MJPGX", "MJ", "MJ G"):
            cfg = config.load("sim")
            cfg["camera"]["fourcc"] = bad
            with self.assertRaises(config.ConfigError) as ctx:
                config.validate(cfg)
            self.assertIn("camera.fourcc", str(ctx.exception))

    def test_rejects_negative_fps(self):
        cfg = config.load("sim")
        cfg["camera"]["fps"] = -1
        with self.assertRaises(config.ConfigError) as ctx:
            config.validate(cfg)
        self.assertIn("camera.fps", str(ctx.exception))


class TestBlankFrameGuardConfig(unittest.TestCase):
    """帧级均匀保护（2026-11-07）：近黑帧会让 YuNet 吐高置信假脸，默认要拦。"""

    def test_default_is_on_with_a_conservative_threshold(self):
        det = config.DEFAULTS["detection"]
        self.assertAlmostEqual(det["blank_std"], 2.0)
        self.assertAlmostEqual(config.load("sim")["detection"]["blank_std"], 2.0)

    def test_board_keeps_it_on(self):
        self.assertGreater(config.load("board")["detection"]["blank_std"], 0.0)

    def test_zero_means_disabled(self):
        cfg = config.load("sim")
        cfg["detection"]["blank_std"] = 0
        config.validate(cfg)

    def test_rejects_negative_threshold(self):
        cfg = config.load("sim")
        cfg["detection"]["blank_std"] = -1.0
        with self.assertRaises(config.ConfigError) as ctx:
            config.validate(cfg)
        self.assertIn("detection.blank_std", str(ctx.exception))


class TestServerTimeoutConfig(unittest.TestCase):
    """WS 出口的超时必须可配且有界（2026-11-07：对端不读会把主循环拖死）。"""

    def test_defaults_are_bounded(self):
        srv = config.DEFAULTS["server"]
        self.assertGreater(srv["timeout"], 0)
        self.assertGreater(srv["send_timeout"], 0)
        self.assertLessEqual(srv["send_timeout"], srv["timeout"])

    def test_board_overrides_are_bounded(self):
        srv = config.load("board")["server"]
        self.assertGreater(srv["timeout"], 0)
        self.assertGreater(srv["send_timeout"], 0)

    def test_rejects_non_positive_values(self):
        for key in ("timeout", "send_timeout"):
            cfg = config.load("sim")
            cfg["server"][key] = 0
            with self.assertRaises(config.ConfigError) as ctx:
                config.validate(cfg)
            self.assertIn("server.%s" % key, str(ctx.exception))


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
