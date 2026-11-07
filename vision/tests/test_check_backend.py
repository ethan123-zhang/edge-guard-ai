"""部署自检脚本的退出码（`scripts/check_backend.py`）。

这个脚本的唯一价值就是「悄悄退到 CPU 时部署失败」，所以重点是退出码不能错：
0 = 符合预期、1 = 建起来了但不是期望后端、2 = 建不起来。
"""
import contextlib
import importlib.util
import io
import os
import unittest

from vision import onnx_pipeline

PROJECT_DIR = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
SCRIPT = os.path.join(PROJECT_DIR, "scripts", "check_backend.py")


def load_script():
    spec = importlib.util.spec_from_file_location("check_backend_under_test", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class FakeComponent(object):
    def __init__(self, accelerator):
        self.backend = "yunet-onnx"
        self.accelerator = accelerator

    def describe(self):
        return "yunet-onnx @ %s" % self.accelerator


class TestCheckBackendExitCodes(unittest.TestCase):
    def setUp(self):
        self._original = onnx_pipeline.create_components
        self.module = load_script()

    def tearDown(self):
        onnx_pipeline.create_components = self._original

    def _run(self, components, expect, fails=False):
        def fake_create(cfg, log=None):
            if fails:
                raise onnx_pipeline.PipelineUnavailable("engine 缺失")
            return components[0], components[1], "onnx"

        onnx_pipeline.create_components = fake_create
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = self.module.main(["--profile", "sim", "--expect", expect])
        return code, out.getvalue()

    def test_passes_when_both_components_use_tensorrt(self):
        code, text = self._run((FakeComponent("tensorrt"), FakeComponent("tensorrt")),
                               "tensorrt")
        self.assertEqual(code, 0, text)
        self.assertIn("yunet-onnx/tensorrt", text)

    def test_fails_when_one_component_silently_fell_back_to_dnn(self):
        code, text = self._run((FakeComponent("tensorrt"), FakeComponent("dnn")),
                               "tensorrt")
        self.assertEqual(code, 1, "退到 dnn 必须让部署失败")
        self.assertIn("不通过", text)

    def test_any_expectation_only_requires_a_working_pipeline(self):
        code, _text = self._run((FakeComponent("dnn"), FakeComponent("dnn")), "any")
        self.assertEqual(code, 0)

    def test_dnn_expectation_rejects_a_tensorrt_pipeline(self):
        code, _text = self._run((FakeComponent("tensorrt"), FakeComponent("tensorrt")),
                                "dnn")
        self.assertEqual(code, 1)

    def test_build_failure_is_exit_code_2(self):
        code, text = self._run(None, "tensorrt", fails=True)
        self.assertEqual(code, 2, text)
        self.assertIn("建不起来", text)


if __name__ == "__main__":
    unittest.main()