"""TensorRT 封装的纯逻辑测试：哈希绑定、版本闸门、为什么必须回退 CPU。

真正的 engine 推理只在板端跑（见 `docs/Jetson-TRT加速评估-2026-11-06.md` 与
`logs/video-validation/trt_regression.py`）。这里用假 engine 文件把"加载前该拦的
都拦住"钉死：哈希对不上、模型换过、sidecar 缺字段、版本漂移，都不能静默跑。
"""
import json
import os
import shutil
import tempfile
import unittest

from vision import trt_backend


class _TempDirCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="trt-backend-")
        self.engine = os.path.join(self.tmp, "model.engine")
        self.model = os.path.join(self.tmp, "model.onnx")
        with open(self.engine, "wb") as fh:
            fh.write(b"fake-engine-bytes")
        with open(self.model, "wb") as fh:
            fh.write(b"fake-onnx-bytes")
        self.sidecar = self.make_sidecar(trt_version="8.2.1")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def make_sidecar(self, trt_version="8.2.1", engine_path=None):
        return trt_backend.build_sidecar(
            engine_path or self.engine, self.model, "input",
            {"min": [1, 3, 320, 320], "opt": [1, 3, 320, 320], "max": [1, 3, 320, 320]},
            fp16=True, outputs=[{"name": "loc", "shape": [5875, 14]}],
            trt_version=trt_version)


class TestSidecar(_TempDirCase):
    def test_sidecar_records_hashes_and_profile(self):
        path = trt_backend.sidecar_path(self.engine)
        self.assertTrue(os.path.exists(path))
        with open(path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
        self.assertEqual(data["schema"], trt_backend.ENGINE_SCHEMA)
        self.assertEqual(data["engine_sha256"], trt_backend.sha256_file(self.engine))
        self.assertEqual(data["model_sha256"], trt_backend.sha256_file(self.model))
        self.assertEqual(data["tensorrt"], "8.2.1")
        self.assertTrue(data["fp16"])
        self.assertEqual(data["input"]["name"], "input")

    def test_sidecar_missing_raises(self):
        os.remove(trt_backend.sidecar_path(self.engine))
        with self.assertRaises(trt_backend.BackendUnavailable):
            trt_backend.verify_engine(self.engine, self.model, expected_tensorrt="8.2.1")

    def test_unknown_schema_rejected(self):
        path = trt_backend.sidecar_path(self.engine)
        with open(path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
        data["schema"] = "sentinel/trt-engine@0"
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(data, fh)
        with self.assertRaises(trt_backend.BackendUnavailable):
            trt_backend.verify_engine(self.engine, self.model, expected_tensorrt="8.2.1")

    def test_missing_required_key_rejected(self):
        path = trt_backend.sidecar_path(self.engine)
        with open(path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
        data.pop("model_sha256")
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(data, fh)
        with self.assertRaises(trt_backend.BackendUnavailable):
            trt_backend.verify_engine(self.engine, self.model, expected_tensorrt="8.2.1")


class TestVerification(_TempDirCase):
    def test_valid_engine_passes(self):
        sidecar = trt_backend.verify_engine(self.engine, self.model, expected_tensorrt="8.2.1")
        self.assertEqual(sidecar["model"], "model.onnx")

    def test_engine_swap_detected(self):
        with open(self.engine, "wb") as fh:
            fh.write(b"different-engine-bytes")
        with self.assertRaises(trt_backend.BackendUnavailable):
            trt_backend.verify_engine(self.engine, self.model, expected_tensorrt="8.2.1")

    def test_model_swap_detected(self):
        with open(self.model, "wb") as fh:
            fh.write(b"another-onnx")
        with self.assertRaises(trt_backend.BackendUnavailable):
            trt_backend.verify_engine(self.engine, self.model, expected_tensorrt="8.2.1")

    def test_tensorrt_version_mismatch_rejected(self):
        with self.assertRaises(trt_backend.BackendUnavailable):
            trt_backend.verify_engine(self.engine, self.model, expected_tensorrt="9.9.9")

    def test_missing_model_rejected(self):
        os.remove(self.model)
        with self.assertRaises(trt_backend.BackendUnavailable):
            trt_backend.verify_engine(self.engine, self.model, expected_tensorrt="8.2.1")


class TestProfile(unittest.TestCase):
    def test_dynamic_batch_inside_profile_ok(self):
        profile = {"min": [1, 3, 112, 112], "opt": [4, 3, 112, 112], "max": [8, 3, 112, 112]}
        for batch in (1, 4, 8):
            self.assertIsNone(trt_backend.check_profile(profile, (batch, 3, 112, 112)))

    def test_batch_above_max_rejected(self):
        profile = {"min": [1, 3, 112, 112], "opt": [4, 3, 112, 112], "max": [8, 3, 112, 112]}
        with self.assertRaises(trt_backend.BackendUnavailable):
            trt_backend.check_profile(profile, (9, 3, 112, 112))

    def test_static_mismatch_rejected(self):
        profile = {"min": [1, 3, 320, 320], "opt": [1, 3, 320, 320], "max": [1, 3, 320, 320]}
        with self.assertRaises(trt_backend.BackendUnavailable):
            trt_backend.check_profile(profile, (1, 3, 640, 640))

    def test_none_profile_is_lenient(self):
        self.assertIsNone(trt_backend.check_profile(None, (1, 3, 320, 320)))


class TestFallback(_TempDirCase):
    def test_create_backend_falls_back_when_version_differs(self):
        self.make_sidecar(trt_version="0.0.0")
        messages = []
        backend = trt_backend.create_backend(
            self.engine, self.model, log=messages.append)
        self.assertIsNone(backend)
        self.assertEqual(len(messages), 1)
        self.assertIn("回退 CPU", messages[0])

    def test_create_backend_falls_back_when_engine_missing(self):
        messages = []
        backend = trt_backend.create_backend(
            os.path.join(self.tmp, "missing.engine"), None, log=messages.append)
        self.assertIsNone(backend)
        self.assertIn("回退 CPU", messages[0])

    def test_tensorrt_version_raises_without_module(self):
        try:
            import tensorrt  # noqa: F401
        except ImportError:
            with self.assertRaises(trt_backend.BackendUnavailable):
                trt_backend.tensorrt_version()
        else:
            self.assertTrue(trt_backend.tensorrt_version())


class TestPy36(unittest.TestCase):
    def test_source_has_no_python37_features(self):
        import ast
        path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "trt_backend.py")
        with open(path, "r", encoding="utf-8") as fh:
            source = fh.read()
        try:
            ast.parse(source, filename=path, feature_version=(3, 6))
        except TypeError:
            self.skipTest("当前解释器不支持 feature_version")


if __name__ == "__main__":
    unittest.main()