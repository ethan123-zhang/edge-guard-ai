"""Python 3.6 语法闸门。

板端是 Ubuntu 18.04 的 **Python 3.6**，`vision/` 里只要冒出一个 3.7 特性，
上板就是 `SyntaxError`，而 PC 上完全看不出来。所以这里用 3.6 语法解析一遍。

原生 Python 3.6 直接用它自己的解析器；较新解释器则传
`feature_version=(3, 6)`。二者都不可用时明确 skip，不假装通过。

（`from __future__ import annotations` 在 3.6 是 SyntaxError，但 ast 未必拦，
所以额外做文本检查。）
"""
import ast
import os
import sys
import unittest

VISION_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PROJECT_DIR = os.path.dirname(VISION_DIR)

# 这些文件要原样跑在板子上（Jetson, Python 3.6）
BOARD_TARGETS = [
    os.path.join(VISION_DIR, name)
    for name in sorted(os.listdir(VISION_DIR))
    if name.endswith(".py")
]
BOARD_TARGETS += [
    os.path.join(PROJECT_DIR, "scripts", "vision_sim.py"),
    os.path.join(PROJECT_DIR, "scripts", "vision_selftest.py"),
    os.path.join(PROJECT_DIR, "scripts", "enroll_faces.py"),
    os.path.join(PROJECT_DIR, "scripts", "check_backend.py"),
    # 现场无显示器：注册/自检全靠这两个纯终端脚本，必须能跑在 3.6 上
    os.path.join(PROJECT_DIR, "scripts", "camera_watch.py"),
    os.path.join(PROJECT_DIR, "scripts", "ensure_engines.py"),
    os.path.join(PROJECT_DIR, "scripts", "video_pipeline_probe.py"),
    os.path.join(PROJECT_DIR, "scripts", "video_stability_probe.py"),
    # 素材录制是 PC 侧交互脚本（需要显示器），但 scripts/ 会整体同步到板端，
    # 所以照样过 3.6 语法闸门，避免板上出现一个连解析都失败的 .py
    os.path.join(PROJECT_DIR, "scripts", "record_camera.py"),
]


def read(path):
    with open(path, "r", encoding="utf-8") as fh:
        return fh.read()


def supports_feature_version():
    """Python 3.6 的 ast.parse 没有 feature_version 参数。"""
    try:
        ast.parse("", feature_version=(3, 6))
    except TypeError:
        return False
    return True


def forbidden_imports(tree):
    """按 AST 判断，而不是文本搜 —— 注释和文档字符串里提到这些词是允许的。"""
    found = []
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            module = node.module or ""
            names = [alias.name for alias in node.names]
            if module == "dataclasses":
                found.append("import dataclasses")
            if module == "__future__" and "annotations" in names:
                found.append("from __future__ import annotations")
        elif isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name == "dataclasses":
                    found.append("import dataclasses")
        elif hasattr(ast, "NamedExpr") and isinstance(node, ast.NamedExpr):
            found.append("海象运算符 :=（Python 3.8+）")
    return found


class TestPy36Compatibility(unittest.TestCase):
    def test_board_targets_exist(self):
        self.assertGreaterEqual(len(BOARD_TARGETS), 8)
        for path in BOARD_TARGETS:
            self.assertTrue(os.path.exists(path), path)

    def test_parses_with_python36_grammar(self):
        native_py36 = sys.version_info[:2] == (3, 6)
        if not native_py36 and not supports_feature_version():
            self.skipTest("当前解释器既不是 Python 3.6，也不支持 feature_version")
        for path in BOARD_TARGETS:
            source = read(path)
            try:
                if native_py36:
                    ast.parse(source, filename=path)
                else:
                    ast.parse(source, filename=path, feature_version=(3, 6))
            except SyntaxError as exc:
                self.fail("%s 用了 Python 3.6 不支持的语法：%s" % (os.path.basename(path), exc))

    def test_no_python37_plus_features(self):
        for path in BOARD_TARGETS:
            found = forbidden_imports(ast.parse(read(path), filename=path))
            self.assertEqual(found, [], "%s 含有 %s" % (os.path.basename(path), found))


if __name__ == "__main__":
    unittest.main()
