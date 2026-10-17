"""跑全部测试：`python -m dialog.run_tests`

不带 pytest 依赖 —— 板子上装不上 pytest，用标准库 unittest 保证
"PC 上能跑 = 板子上也能跑"。
"""
import sys
import unittest


def main() -> int:
    loader = unittest.TestLoader()
    suite = loader.discover("dialog/tests", top_level_dir=".")
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    return 0 if result.wasSuccessful() else 1


if __name__ == "__main__":
    sys.exit(main())
