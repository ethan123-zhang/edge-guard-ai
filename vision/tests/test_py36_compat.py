"""Python 3.6 语法闸门。

板端是 Ubuntu 18.04 的 **Python 3.6**，`vision/` 里只要冒出一个 3.7 特性，
上板就是 `SyntaxError`，而 PC 上完全看不出来。所以这里用 3.6 语法解析一遍。

（`from __future__ import annotations` 在 3.6 是 SyntaxError，但 ast 未必拦，
所以额外做文本检查。）
"""
import ast
import os
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
]


def read(path):
    with open(path, "r", encoding="utf-8") as fh:
        return fh.read()


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
        elif isinstance(node, ast.NamedExpr):
            found.append("海象运算符 :=（Python 3.8+）")
    return found


class TestPy36Compatibility(unittest.TestCase):
    def test_board_targets_exist(self):
        self.assertGreaterEqual(len(BOARD_TARGETS), 8)
        for path in BOARD_TARGETS:
            self.assertTrue(os.path.exists(path), path)

    def test_parses_with_python36_grammar(self):
        for path in BOARD_TARGETS:
            source = read(path)
            try:
                ast.parse(source, filename=path, feature_version=(3, 6))
            except SyntaxError as exc:
                self.fail("%s 用了 Python 3.6 不支持的语法：%s" % (os.path.basename(path), exc))

    def test_no_python37_plus_features(self):
        for path in BOARD_TARGETS:
            found = forbidden_imports(ast.parse(read(path), filename=path))
            self.assertEqual(found, [], "%s 含有 %s" % (os.path.basename(path), found))


if __name__ == "__main__":
    unittest.main()
