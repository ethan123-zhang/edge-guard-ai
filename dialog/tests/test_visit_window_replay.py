"""回放脚本的断言（`scripts/visit_window_replay.py`）。

脚本的价值在于"同一份证据既能当场断言、又能落盘给复核"，所以这里只钉 3 件事：
① 合成的已知人脸时间线全部断言通过；② 两个匿名 ID 不会被并成一次；③ 回放能用
指定的 `msg_id` 复现"同一条消息重传"（否则幂等那条检查是假绿）。
"""
import importlib.util
import os
import unittest

PROJECT_DIR = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
SCRIPT = os.path.join(PROJECT_DIR, "scripts", "visit_window_replay.py")
FIXTURE = os.path.join(PROJECT_DIR, "logs", "video-validation", "natural-head-full.json")


def load_script():
    spec = importlib.util.spec_from_file_location("visit_window_replay_under_test", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class TestReplayScript(unittest.TestCase):
    def setUp(self):
        self.module = load_script()

    def test_known_timeline_all_checks_pass(self):
        report = self.module.replay_known(self.module.visits.WINDOW_MS)
        failed = [item["label"] for item in report["checks"] if not item["ok"]]
        self.assertEqual(failed, [], "合成时间线的断言应该全过")
        self.assertEqual(report["counts"]["visit_rows"], 2)
        self.assertEqual(report["counts"]["person_visit_count"], 2)

    def test_unknown_pair_checks_pass(self):
        report = self.module.replay_unknown_pair(self.module.visits.WINDOW_MS)
        failed = [item["label"] for item in report["checks"] if not item["ok"]]
        self.assertEqual(failed, [])
        self.assertEqual(report["visits"], 2)

    def test_event_helper_can_replay_a_duplicate_msg_id(self):
        msg = self.module._event("p_1", self.module.BASE, "m-fixed", is_known=True,
                                 name="王阿姨")
        self.assertEqual(msg["msg_id"], "m-fixed")

    @unittest.skipUnless(os.path.exists(FIXTURE), "本机没有自然姿态夹具（gitignored）")
    def test_natural_fixture_is_not_merged_by_business_layer(self):
        report = self.module.replay_natural(FIXTURE, self.module.visits.WINDOW_MS)
        self.assertEqual(report["protocol_violations"], 0)
        self.assertEqual(report["event_count"], 12)
        self.assertEqual(report["visits_created"], report["event_count"],
                         "全是不同 unk_ID：业务层不得替视觉层猜同一个人")
        self.assertEqual(report["reused"], 0)


if __name__ == "__main__":
    unittest.main()