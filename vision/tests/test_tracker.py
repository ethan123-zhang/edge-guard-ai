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


# 2Hz 采样时刻（毫秒）：一次调用代表一个采样帧
TWO_HZ_TS = (0, 500, 1000, 1500, 2000, 2500)


def counts(per_frame):
    """每帧事件数，方便直接和预期序列比较。"""
    return [len(events) for events in per_frame]


class TestConsecutiveConfirmation(unittest.TestCase):
    """连续确认：只数**当前连续段**的命中，漏检/换标签一律打断。

    背景：旧实现按标签累计整条轨道的票数，导致"有、有、无、有、有、有"
    在第三次命中（1.5s）就确认，而不是连续三帧（2.5s）。
    """

    def test_gap_before_confirmation_restarts_count(self):
        """有、有、无、有、有、有：只在 2500ms 确认，且 TTL 内复用同一轨道。"""
        tracker = FaceTracker(confirm_frames=3)
        present = (True, True, False, True, True, True)
        per_frame = []
        for ts, seen in zip(TWO_HZ_TS, present):
            per_frame.append(tracker.update([face()] if seen else [], ts))

        self.assertEqual(counts(per_frame), [0, 0, 0, 0, 0, 1])
        event = per_frame[-1][0]
        self.assertEqual(event.frame_ts, 2500)
        self.assertEqual(event.track_id, 1, "TTL 内保留同一空间轨道，不新建 track")
        self.assertTrue(event.face_id.startswith("unk_0"), "未知身份 ID 在 TTL 内保持")
        self.assertEqual(tracker.track_count, 1)

    def test_identity_switch_does_not_accumulate_old_votes(self):
        """A,A,B,A,A,A：A 的旧票不能和 B 的票凑数，最后一帧才确认 A。"""
        tracker = FaceTracker(confirm_frames=3)
        labels = ("A", "A", "B", "A", "A", "A")
        names = {"A": "甲", "B": "乙"}
        scores = (0.90, 0.90, 0.80, 0.70, 0.70, 0.70)
        per_frame = []
        for ts, label, score in zip(TWO_HZ_TS, labels, scores):
            per_frame.append(tracker.update(
                [face(face_id="p_" + label, name=names[label], score=score)], ts))

        self.assertEqual(counts(per_frame), [0, 0, 0, 0, 0, 1])
        event = per_frame[-1][0]
        self.assertEqual(event.frame_ts, 2500)
        self.assertEqual(event.face_id, "p_A")
        self.assertEqual(event.name, "甲")
        self.assertAlmostEqual(event.confidence, 0.70)

    def test_known_unknown_round_trip_restarts_segment(self):
        """known,known,unknown,unknown,known：旧段票数不累计。"""
        tracker = FaceTracker(confirm_frames=3)
        frames = [
            [face(face_id="p_9", name="张三", score=0.90)],
            [face(face_id="p_9", name="张三", score=0.90)],
            [face(score=0.60)],
            [face(score=0.60)],
            [face(face_id="p_9", name="张三", score=0.90)],
        ]
        per_frame = [tracker.update(f, ts) for f, ts in zip(frames, TWO_HZ_TS)]

        self.assertEqual(counts(per_frame), [0, 0, 0, 0, 0])
        self.assertEqual(tracker.update(
            [face(face_id="p_9", name="张三", score=0.90)], 3000), [])
        emitted = tracker.update(
            [face(face_id="p_9", name="张三", score=0.90)], 3500)
        self.assertEqual(len(emitted), 1)
        self.assertEqual(emitted[0].name, "张三")

    def test_unknown_segment_after_known_segment_reports_unknown(self):
        """同一轨道里 known→unknown：事件必须是当前段的未知身份。"""
        tracker = FaceTracker(confirm_frames=3)
        frames = [
            [face(face_id="p_9", name="张三", score=0.90)],
            [face(face_id="p_9", name="张三", score=0.90)],
            [face(score=0.60)],
            [face(score=0.60)],
            [face(score=0.60)],
        ]
        per_frame = [tracker.update(f, ts) for f, ts in zip(frames, TWO_HZ_TS)]

        self.assertEqual(counts(per_frame), [0, 0, 0, 0, 1])
        event = per_frame[-1][0]
        self.assertFalse(event.is_known)
        self.assertIsNone(event.name)
        self.assertTrue(event.face_id.startswith("unk_0"))
        self.assertAlmostEqual(event.confidence, 0.60)

    def test_stale_score_and_name_do_not_leak_into_new_segment(self):
        """漏检前的 0.97 分和旧姓名不能污染新连续段的事件。"""
        tracker = FaceTracker(confirm_frames=3)
        frames = [
            [face(face_id="p_7", name="旧名字", score=0.97)],
            [face(face_id="p_7", name="旧名字", score=0.97)],
            [],
            [face(face_id="p_7", name="新名字", score=0.52)],
            [face(face_id="p_7", name="新名字", score=0.52)],
            [face(face_id="p_7", name="新名字", score=0.52)],
        ]
        per_frame = [tracker.update(f, ts) for f, ts in zip(frames, TWO_HZ_TS)]

        self.assertEqual(counts(per_frame), [0, 0, 0, 0, 0, 1])
        event = per_frame[-1][0]
        self.assertEqual(event.name, "新名字")
        self.assertAlmostEqual(event.confidence, 0.52)

    def test_one_missing_target_does_not_reset_the_other(self):
        """两个目标里只有一个漏检：另一个照常确认，漏检的那个重新数。"""
        tracker = FaceTracker(confirm_frames=3)
        left = face((100, 100, 80, 80))
        right = face((600, 100, 80, 80))
        frames = [
            [left, right],
            [left, right],
            [left],          # 右目标在第 3 帧漏检
            [left, right],
            [left, right],
        ]
        per_frame = [tracker.update(f, ts) for f, ts in zip(frames, TWO_HZ_TS)]
        emitted = [event for events in per_frame for event in events]

        self.assertEqual(len(emitted), 1, "只有连续命中的左目标可以确认")
        self.assertEqual(emitted[0].bbox, (100, 100, 80, 80))
        self.assertEqual(emitted[0].frame_ts, 1000)
        self.assertEqual(sum(counts(per_frame[3:])), 0,
                         "右目标漏检后必须重新连续 3 帧")

    def test_confirmed_track_does_not_reemit_after_short_gap(self):
        """已确认轨道在 TTL 内短空白后恢复：不重复上报。"""
        tracker = FaceTracker(confirm_frames=3)
        present = (True, True, True, False, False, True, True, True)
        per_frame = []
        for ts, seen in zip((0, 500, 1000, 1500, 2000, 2500, 3000, 3500), present):
            per_frame.append(tracker.update([face()] if seen else [], ts))

        self.assertEqual(counts(per_frame), [0, 0, 1, 0, 0, 0, 0, 0])
        self.assertEqual(per_frame[2][0].track_id, 1)
        self.assertEqual(tracker.track_count, 1, "短空白不丢空间轨道")

    def test_confirm_frames_one_emits_on_first_matching_frame(self):
        """confirm_frames=1 仍然有效：首次命中即确认。"""
        tracker = FaceTracker(confirm_frames=1)
        self.assertEqual(counts([tracker.update([face()], 0)]), [1])
        self.assertEqual(counts([tracker.update([face((700, 100, 80, 80))], 500)]), [1])


class TestTrackTtlBoundary(unittest.TestCase):
    """TTL 恰好边界沿用 <= 保留，超过边界才新建轨道。"""

    def test_exact_ttl_boundary_keeps_track_and_restarts_streak(self):
        tracker = FaceTracker(confirm_frames=3, track_ttl_sec=2)
        tracker.update([face()], 0)
        tracker.update([face()], 500)

        self.assertEqual(tracker.update([], 2500), [], "恰好 2000ms：轨道保留")
        self.assertEqual(tracker.track_count, 1)
        self.assertEqual(tracker.update([face()], 2500), [])
        self.assertEqual(tracker.update([face()], 3000), [])
        emitted = tracker.update([face()], 3500)
        self.assertEqual(len(emitted), 1)
        self.assertEqual(emitted[0].track_id, 1, "边界内复用同一 track_id")

    def test_just_over_ttl_boundary_starts_new_track(self):
        tracker = FaceTracker(confirm_frames=3, track_ttl_sec=2)
        tracker.update([face()], 0)
        tracker.update([face()], 500)

        self.assertEqual(tracker.update([], 2501), [], "2001ms > 2000ms：轨道淘汰")
        self.assertEqual(tracker.track_count, 0)
        for ts in (2501, 3001, 3501):
            emitted = tracker.update([face()], ts)
        self.assertEqual(len(emitted), 1)
        self.assertEqual(emitted[0].track_id, 2, "超过边界后才新建轨道")
        self.assertTrue(emitted[0].face_id.startswith("unk_2501"))


class TestTrackIdAnnotation(unittest.TestCase):
    """tracker 要把 track_id 回填进 face（关联层按轨道收外观证据）。"""

    def test_new_track_annotates_face(self):
        tracker = FaceTracker(confirm_frames=1)
        item = face()
        tracker.update([item], 1000)
        self.assertEqual(item["track_id"], 1)

    def test_matched_track_keeps_the_same_id(self):
        tracker = FaceTracker(confirm_frames=1)
        first = face()
        tracker.update([first], 1000)
        second = face(bbox=(102, 102, 80, 80))
        tracker.update([second], 1500)
        self.assertEqual(second["track_id"], first["track_id"])

    def test_event_track_id_matches_the_face(self):
        tracker = FaceTracker(confirm_frames=1)
        item = face()
        emitted = tracker.update([item], 1000)
        self.assertEqual(len(emitted), 1)
        self.assertEqual(emitted[0].track_id, item["track_id"])

    def test_extra_keys_on_face_are_ignored(self):
        """service 会往 face 里塞 embedding / rms_norm，tracker 不该受影响。"""
        tracker = FaceTracker(confirm_frames=1)
        item = face()
        item["embedding"] = [0.0] * 128
        item["rms_norm"] = 0.05
        self.assertEqual(len(tracker.update([item], 1000)), 1)


if __name__ == "__main__":
    unittest.main()
