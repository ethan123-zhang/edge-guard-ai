"""跑视觉节点全部测试：`python -m vision.run_tests`（不带 pytest 依赖）。"""
import sys
import unittest


def main():
    suite = unittest.TestLoader().discover("vision/tests", top_level_dir=".")
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    return 0 if result.wasSuccessful() else 1


if __name__ == "__main__":
    sys.exit(main())
