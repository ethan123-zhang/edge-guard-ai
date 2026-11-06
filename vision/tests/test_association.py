"""短期匿名关联层测试：唯一证据才复用会话，其余情况保留独立候选。

这些用例对应 `docs/跨轨关联与两层去重合同-2026-11-01.md` 的"短期匿名关联"行，
也是"相邻双人不抢同一会话 / 同位置换人不得继承上报"这两类翻车的防火墙。
"""
import math
import unittest

from vision.association import AnonymousAssociator


def vec(angle_deg):
    """二维单位向量：与 (1, 0) 的余弦 = cos(angle_deg)。"""
    rad = math.radians(angle_deg)
    return (math.cos(rad), math.sin(rad))


def same_angle(cosine):
    return math.degrees(math.acos(cosine))


class TestDisabledByDefault(unittest.TestCase):
    def test_disabled_does_not_touch_state(self):
        assoc = AnonymousAssociator()
        result = assoc.observe(1, vec(0), 0.02, 1000)
        self.assertFalse(result.associated)
        self.assertEqual(result.reason, "disabled")
        self.assertIsNone(result.session_id)
        self.assertEqual(assoc.session_count, 0)

    def test_can_be_switched_on_later(self):
        assoc = AnonymousAssociator()
        assoc.enabled = True
        result = assoc.observe(1, vec(0), 0.02, 1000)
        self.assertEqual(result.reason, "new_session")
        self.assertIsNotNone(result.session_id)


class TestInvalidEvidence(unittest.TestCase):
    def test_none_embedding_is_rejected(self):
        assoc = AnonymousAssociator(enabled=True)
        result = assoc.observe(1, None, 0.02, 1000)
        self.assertEqual(result.reason, "invalid_evidence")
        self.assertEqual(assoc.session_count, 0)

    def test_empty_and_nan_embeddings_are_rejected(self):
        assoc = AnonymousAssociator(enabled=True)
        self.assertEqual(assoc.observe(1, (), 0.02, 1000).reason, "invalid_evidence")
        self.assertEqual(assoc.observe(1, (float("nan"), 0.0), 0.02, 1000).reason,
                         "invalid_evidence")
        self.assertEqual(assoc.observe(1, ("x", 1.0), 0.02, 1000).reason,
                         "invalid_evidence")

    def test_zero_vector_is_rejected(self):
        assoc = AnonymousAssociator(enabled=True)
        result = assoc.observe(1, (0.0, 0.0), 0.02, 1000)
        self.assertEqual(result.reason, "invalid_evidence")
        self.assertEqual(assoc.session_count, 0)


class TestReuseRequiresUniqueEvidence(unittest.TestCase):
    def test_same_face_after_gap_reuses_session(self):
        assoc = AnonymousAssociator(enabled=True)
        first = assoc.observe(1, vec(0), 0.02, 1000)
        self.assertEqual(first.reason, "new_session")
        # 换了一条空间轨道（track_id 不同），外观一致 → 应复用会话
        second = assoc.observe(2, vec(0), 0.02, 5000)
        self.assertTrue(second.associated)
        self.assertEqual(second.reason, "associated")
        self.assertEqual(second.session_id, first.session_id)
        self.assertEqual(assoc.session_count, 1)

    def test_different_face_creates_new_session(self):
        assoc = AnonymousAssociator(enabled=True)
        first = assoc.observe(1, vec(0), 0.02, 1000)
        second = assoc.observe(2, vec(90), 0.02, 5000)
        self.assertFalse(second.associated)
        self.assertIn(second.reason, ("below_tau", "new_session"))
        self.assertNotEqual(second.session_id, first.session_id)
        self.assertEqual(assoc.session_count, 2)

    def test_below_tau_creates_new_session(self):
        assoc = AnonymousAssociator(enabled=True)
        assoc.observe(1, vec(0), 0.02, 1000)
        # 与参考余弦 = 0.40 < tau 0.45
        result = assoc.observe(2, vec(same_angle(0.40)), 0.02, 5000)
        self.assertFalse(result.associated)
        self.assertEqual(result.reason, "below_tau")
        self.assertAlmostEqual(result.own_best, 0.40, places=6)

    def test_exactly_delta_passes(self):
        assoc = AnonymousAssociator(enabled=True)
        # 会话 A 参考在 0°，会话 B 参考在 113.13°；查询在 53.13°
        # → 与 A 像 0.60、与 B 像 0.50，差恰好 0.10 = delta，应放行（要求 >=）
        assoc.observe(1, vec(0), 0.02, 1000)
        assoc.observe(2, vec(53.13 + 60.0), 0.02, 5000)
        result = assoc.observe(3, vec(53.13), 0.02, 9000)
        self.assertTrue(result.associated)
        self.assertEqual(result.reason, "associated")
        self.assertAlmostEqual(result.margin, 0.10, places=3)

    def test_ambiguous_margin_keeps_separate_candidate(self):
        assoc = AnonymousAssociator(enabled=True, tau=0.60, margin=0.10)
        # 两个参考与查询分别像 0.80 / 0.75 → 差 0.05 < 0.10
        query = vec(36.87)                      # 与 0° 参考像 0.80
        b = vec(36.87 + 41.41)                  # 与查询像 0.75
        assoc.observe(1, vec(0), 0.02, 1000)
        assoc.observe(2, b, 0.02, 5000)
        result = assoc.observe(3, query, 0.02, 9000)
        self.assertFalse(result.associated)
        self.assertEqual(result.reason, "ambiguous_margin")
        self.assertEqual(assoc.session_count, 3)


class TestOneToOneOccupancy(unittest.TestCase):
    def test_two_live_tracks_never_share_one_session(self):
        assoc = AnonymousAssociator(enabled=True)
        first = assoc.observe(1, vec(0), 0.02, 1000)
        # 相邻双人：第二条轨道还在第一条轨道的 TTL 内，即便外观相同也不能抢会话
        second = assoc.observe(2, vec(0), 0.02, 1100)
        self.assertFalse(second.associated)
        self.assertEqual(second.reason, "session_occupied")
        self.assertNotEqual(second.session_id, first.session_id)
        self.assertEqual(assoc.session_count, 2)

    def test_same_track_keeps_its_session_without_redeciding(self):
        assoc = AnonymousAssociator(enabled=True)
        first = assoc.observe(1, vec(0), 0.02, 1000)
        again = assoc.observe(1, vec(0), 0.02, 1100)
        self.assertTrue(again.associated)
        self.assertEqual(again.reason, "track_bound")
        self.assertEqual(again.session_id, first.session_id)
        self.assertEqual(assoc.session_count, 1)


class TestQualityGateForEnrollment(unittest.TestCase):
    def test_low_quality_face_is_not_enrolled(self):
        assoc = AnonymousAssociator(enabled=True)
        low = assoc.observe(1, vec(0), 0.25, 1000)   # 0.25 > 0.10：不建参考
        self.assertEqual(low.reason, "new_session")
        # 参考库为空 → 后来的同脸也匹配不上，只能新建（宁缺毋滥）
        again = assoc.observe(2, vec(0), 0.02, 5000)
        self.assertEqual(again.reason, "new_session")
        self.assertNotEqual(again.session_id, low.session_id)

    def test_missing_quality_metric_is_not_enrolled(self):
        assoc = AnonymousAssociator(enabled=True)
        assoc.observe(1, vec(0), None, 1000)
        result = assoc.observe(2, vec(0), None, 5000)
        self.assertEqual(result.reason, "new_session")

    def test_better_quality_replaces_worst_reference(self):
        assoc = AnonymousAssociator(enabled=True, max_refs=1)
        first = assoc.observe(1, vec(0), 0.09, 1000)
        # 会话 1 满库后，用质量更好的同人脸替换最差参考（0.05 < 0.09）
        assoc.observe(2, vec(same_angle(0.99)), 0.05, 5000)
        # 现在参考是 vec(≈8°)，用它匹配应能关联；用旧角度则关联不上
        near = assoc.observe(3, vec(same_angle(0.99)), 0.02, 9000)
        self.assertEqual(near.session_id, first.session_id)

    def test_reference_library_is_bounded(self):
        assoc = AnonymousAssociator(enabled=True, max_refs=3)
        assoc.observe(1, vec(0), 0.02, 1000)
        for angle in (5, 10, 15):
            assoc.observe(2, vec(angle), 0.02, 5000)
        for session in assoc._sessions.values():
            self.assertLessEqual(len(session.refs), 3)


class TestLifecycleLimits(unittest.TestCase):
    def test_session_expires_after_ttl(self):
        assoc = AnonymousAssociator(enabled=True, session_ttl_sec=120)
        first = assoc.observe(1, vec(0), 0.02, 0)
        later = assoc.observe(2, vec(0), 0.02, 121000)
        self.assertEqual(later.reason, "new_session")
        self.assertNotEqual(later.session_id, first.session_id)

    def test_capacity_evicts_oldest_session(self):
        assoc = AnonymousAssociator(enabled=True, max_sessions=2)
        ids = []
        for index, angle in enumerate((0, 90, 180)):
            ids.append(assoc.observe(index + 1, vec(angle), 0.02, 1000 + index * 1000).session_id)
        self.assertEqual(assoc.session_count, 2)
        self.assertIn(ids[1], assoc.session_ids())
        self.assertIn(ids[2], assoc.session_ids())
        self.assertNotIn(ids[0], assoc.session_ids())

    def test_reset_clears_everything(self):
        assoc = AnonymousAssociator(enabled=True)
        assoc.observe(1, vec(0), 0.02, 1000)
        assoc.reset()
        self.assertEqual(assoc.session_count, 0)
        self.assertEqual(assoc.session_ids(), [])
        again = assoc.observe(1, vec(0), 0.02, 1000)
        self.assertEqual(again.session_id, 1)


class TestThresholdValidation(unittest.TestCase):
    def test_illegal_values_are_rejected(self):
        with self.assertRaises(ValueError):
            AnonymousAssociator(tau=0.0)
        with self.assertRaises(ValueError):
            AnonymousAssociator(tau=1.5)
        with self.assertRaises(ValueError):
            AnonymousAssociator(margin=-0.01)
        with self.assertRaises(ValueError):
            AnonymousAssociator(session_ttl_sec=0)
        with self.assertRaises(ValueError):
            AnonymousAssociator(max_sessions=0)
        with self.assertRaises(ValueError):
            AnonymousAssociator(max_refs=0)


if __name__ == "__main__":
    unittest.main()