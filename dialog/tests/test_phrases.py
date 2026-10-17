"""固定话术测试（docs/03 §5.4、§6；验收项 M6）。

2026-10-17 板端实测：提示词从 1425 B 涨到 3022 B，全是在跟模型反复商量
"什么时候该说转达"。结论是这句话根本不归 LLM 管 —— 它必须由代码一字不改地说出来。
"""
import unittest

from dialog import phrases


class TestHandoff(unittest.TestCase):
    """M6：收尾播报必须是确定的。"""

    def test_handoff_with_name_matches_the_spec_verbatim(self):
        self.assertEqual(
            phrases.handoff("李先生"),
            "好的，已经帮您转告家里人了，李先生再见。",
        )

    def test_handoff_without_name_still_closes_the_conversation(self):
        self.assertEqual(phrases.handoff(None), "好的，已经帮您转告家里人了，再见。")

    def test_handoff_ignores_blank_name(self):
        self.assertEqual(phrases.handoff("   "), phrases.handoff(None))

    def test_handoff_is_deterministic(self):
        # 同一输入连说 5 次必须字字相同 —— 这是它跟 LLM 生成话术的根本区别
        outs = {phrases.handoff("李先生") for _ in range(5)}
        self.assertEqual(len(outs), 1)


class TestGreeting(unittest.TestCase):
    def test_unknown_visitor_gets_generic_greeting(self):
        self.assertIn("请问您找哪位", phrases.greeting(None))

    def test_known_visitor_is_greeted_by_name(self):
        self.assertIn("张阿姨", phrases.greeting("张阿姨", visit_count=1))

    def test_repeat_visitor_gets_visit_count(self):
        # 熟人记忆（核心亮点 ①）的最小可见形式
        self.assertIn("第 3 次", phrases.greeting("张阿姨", visit_count=3))

    def test_first_visit_does_not_mention_count(self):
        self.assertNotIn("次", phrases.greeting("张阿姨", visit_count=1))


class TestErrorPhrases(unittest.TestCase):
    def test_not_heard_asks_to_repeat(self):
        self.assertIn("再说一遍", phrases.NOT_HEARD)

    def test_silence_exit_records_the_visit(self):
        self.assertIn("记下", phrases.SILENCE_EXIT)

    def test_llm_timeout_does_not_break_flow(self):
        self.assertTrue(phrases.LLM_TIMEOUT)


if __name__ == "__main__":
    unittest.main()
