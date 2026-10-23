"""跟踪稳定性测试：连续 N 帧确认、冷却、一次停留只上报一次。

这些用例对应 docs/02 §3.1 的"发送时机"，也是"门口站一下就刷出几十条消息"
这类翻车的防火墙。
"""
import unittest

from vision.tracker import FaceTracker, iou


def face(bbox=(100, 100, 80, 80), face_id=None, name=None, score=0.9):
    return {"bbox": bbox, "face_id": face_id, "name": name, "score": score}


class TestIou(unittest.TestCase):
    def test_identical_boxes(self):
        self.assertAlmostEqual(iou((0, 0, 10, 10), (0, 0, 10, 10)), 1.0)

    def test_disjoint_boxes(self):
        self.assertEqual(iou((0, 0, 10, 10), (100, 100, 10, 10)), 0.0)

    def test_half_overlap(self):
        self.assertAlmostEqual(iou((0, 0, 10, 10), (5, 0, 10, 10)), 1 / 3.0, places=4)


class TestConfirmation(unittest.TestCase):
    def test_two_frames_are_not_enough(self):
        tracker = FaceTracker(confirm_frames=3)
        self.assertEqual(tracker.update([face()], 1000), [])
        self.assertEqual(tracker.update([face()], 1033), [])

    def test_third_frame_emits_exactly_once(self):
        tracker = FaceTracker(confirm_frames=3)
        tracker.update([face()], 1000)
        tracker.update([face()], 1033)
        emitted = tracker.update([face()], 1066)
        self.assertEqual(len(emitted), 1)
        self.assertEqual(tracker.update([face()], 1099), [])

    def test_unknown_id_follows_the_protocol_format(self):
        tracker = FaceTracker(confirm_frames=1)
        emitted = tracker.update([face()], 1735689600000)
        self.assertTrue(emitted[0].face_id.startswith("unk_1735689600000"))
        self.assertFalse(emitted[0].is_known)
        self.assertIsNone(emitted[0].name)

    def test_known_face_carries_name_and_score(self):
        tracker = FaceTracker(confirm_frames=1)
        emitted = tracker.update([face(face_id="p_10023", name="王阿姨", score=0.87)], 1000)
        self.assertTrue(emitted[0].is_known)
        self.assertEqual(emitted[0].name, "王阿姨")
        self.assertAlmostEqual(emitted[0].confidence, 0.87)

    def test_two_people_in_one_frame_produce_two_events(self):
        tracker = FaceTracker(confirm_frames=1)
        emitted = tracker.update(
            [face((100, 100, 80, 80)), face((600, 100, 80, 80))], 1000)
        self.assertEqual(len(emitted), 2)
        self.assertNotEqual(emitted[0].face_id, emitted[1].face_id)


class TestCooldown(unittest.TestCase):
    def test_same_person_within_cooldown_is_suppressed(self):
        tracker = FaceTracker(confirm_frames=1, cooldown_sec=10)
        first = tracker.update([face(face_id="p_1", name="A")], 1000)
        self.assertEqual(len(first), 1)
        # 人离开了又回来（新轨道、不同位置），但还在 10 秒冷却内
        again = tracker.update([face(bbox=(700, 100, 80, 80), face_id="p_1", name="A")], 3000)
        self.assertEqual(again, [])

    def test_allowed_again_after_cooldown(self):
        tracker = FaceTracker(confirm_frames=1, cooldown_sec=10)
        tracker.update([face(face_id="p_1", name="A")], 1000)
        later = tracker.update([face(bbox=(700, 100, 80, 80), face_id="p_1", name="A")], 12000)
        self.assertEqual(len(later), 1)


class TestTracking(unittest.TestCase):
    def test_moving_face_stays_on_one_track(self):
        tracker = FaceTracker(confirm_frames=3)
        emitted = []
        for step in range(4):
            emitted.extend(tracker.update([face((100 + step * 8, 100, 80, 80))], 1000 + step * 33))
        self.assertEqual(len(emitted), 1)

    def test_stale_track_expires(self):
        tracker = FaceTracker(confirm_frames=3, track_ttl_sec=2)
        tracker.update([face()], 1000)
        self.assertEqual(tracker.track_count, 1)
        tracker.update([], 5000)
        self.assertEqual(tracker.track_count, 0)

    def test_reset_clears_cooldown(self):
        tracker = FaceTracker(confirm_frames=1, cooldown_sec=10)
        tracker.update([face(face_id="p_1", name="A")], 1000)
        tracker.reset()
        self.assertEqual(len(tracker.update([face(face_id="p_1", name="A")], 1500)), 1)


if __name__ == "__main__":
    unittest.main()
