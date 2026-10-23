"""协议字段测试：docs/02 §3 的必填项、毫秒时间戳、陌生人 name=null。

这些断言就是"联调时不用吵"的依据：字段错了单测先红。
"""
import unittest

from vision import events


class TestFaceEvent(unittest.TestCase):
    def test_has_every_required_field(self):
        msg = events.face_event("p_10023", True, "王阿姨", 0.87, (320, 180, 240, 260),
                                frame_ts=1735689599800)
        for key in events.FACE_EVENT_REQUIRED:
            self.assertIn(key, msg)
        self.assertEqual(msg["type"], "face_event")
        self.assertEqual(events.check(msg), [])

    def test_timestamp_is_milliseconds(self):
        msg = events.face_event("p_1", True, "A", 0.5, frame_ts=1735689599800)
        self.assertGreater(msg["ts"], 10 ** 12)      # 10^12 以下就是秒，别混
        self.assertGreater(msg["frame_ts"], 10 ** 12)

    def test_unknown_visitor_has_null_name(self):
        msg = events.face_event("unk_1735689600000", False, None, 0.41, (1, 2, 3, 4))
        self.assertIsNone(msg["name"])
        self.assertFalse(msg["is_known"])
        self.assertEqual(events.check(msg), [])

    def test_confidence_is_clamped_and_rounded(self):
        self.assertEqual(events.face_event("p", True, "A", 1.7)["confidence"], 1.0)
        self.assertEqual(events.face_event("p", True, "A", -0.2)["confidence"], 0.0)
        self.assertEqual(events.face_event("p", True, "A", 0.123456)["confidence"], 0.123)

    def test_bbox_is_ints(self):
        msg = events.face_event("p", True, "A", 0.5, (320.6, 180.2, 240.9, 260.1))
        self.assertEqual(msg["bbox"], [321, 180, 241, 260])

    def test_msg_id_is_unique(self):
        ids = {events.face_event("p", True, "A", 0.5)["msg_id"] for _ in range(50)}
        self.assertEqual(len(ids), 50)


class TestCheck(unittest.TestCase):
    def test_catches_missing_field(self):
        msg = events.face_event("p", True, "A", 0.5)
        del msg["frame_ts"]
        self.assertTrue(any("frame_ts" in p for p in events.check(msg)))

    def test_catches_seconds_instead_of_millis(self):
        msg = events.face_event("p", True, "A", 0.5, ts=1735689600)
        self.assertTrue(any("秒" in p for p in events.check(msg)))

    def test_catches_known_without_name(self):
        msg = events.face_event("p", True, None, 0.5)
        self.assertTrue(events.check(msg))

    def test_catches_stranger_with_name(self):
        msg = events.face_event("unk_1", False, "王阿姨", 0.4)
        self.assertTrue(events.check(msg))

    def test_heartbeat_is_valid(self):
        msg = events.heartbeat(12.34, 4)
        self.assertEqual(events.check(msg), [])
        self.assertEqual(msg["fps"], 12.3)


if __name__ == "__main__":
    unittest.main()
