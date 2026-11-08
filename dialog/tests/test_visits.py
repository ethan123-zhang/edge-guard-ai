"""业务层 10 分钟到访窗口测试（docs/02 §3.1/§4.1，跨轨关联与两层去重合同 §业务窗口）。

这一层决定「第 N 次来」这个答辩数字，所以两条边界必须钉死：
- **固定窗口**：599999ms 算同一次、600000ms 另建；窗口不随新事件滑动延长；
- **活跃到访不拆**：人一直站着（>10 分钟）时再来事件，不能凭计时算成第二次到访。
"""
import tempfile
import unittest
from pathlib import Path

from dialog import db, visits

WINDOW = visits.WINDOW_MS          # 600000
BASE = 1735689600000               # 固定的毫秒基准，测试不依赖真实时钟


def face_event(face_id="p_10023", frame_ts=BASE, msg_id="m-1", is_known=True,
               name="王阿姨", confidence=0.9, **extra):
    msg = {
        "type": "face_event",
        "msg_id": msg_id,
        "ts": frame_ts,
        "face_id": face_id,
        "is_known": is_known,
        "name": name,
        "confidence": confidence,
        "frame_ts": frame_ts,
    }
    msg.update(extra)
    return msg


class VisitBookTestCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.db_path = Path(self._tmp.name) / "sentinel.db"
        self.conn = db.connect(self.db_path)
        db.init_schema(self.conn)
        self.book = visits.VisitBook(self.conn, home_state="empty")

    def tearDown(self):
        self.conn.close()
        self._tmp.cleanup()

    def arrive(self, frame_ts, face_id="p_10023", **kw):
        msg_id = kw.pop("msg_id", "m-%s-%s" % (face_id, frame_ts))
        return self.book.arrive(face_event(face_id=face_id, frame_ts=frame_ts,
                                           msg_id=msg_id, **kw))


class TestNewVisit(VisitBookTestCase):
    def test_first_event_creates_a_visit_and_counts_one(self):
        decision = self.arrive(BASE)
        self.assertTrue(decision.created)
        self.assertEqual(decision.reason, visits.REASON_NEW)
        self.assertEqual(decision.person_visit_count, 1)
        self.assertRegex(decision.visit_id, r"^v_\d{8}_\d{4}$")
        row = db.get_visit(self.conn, decision.visit_id)
        self.assertEqual(row["face_id"], "p_10023")
        self.assertEqual(row["arrived_at"], BASE)
        self.assertEqual(row["home_state"], "empty")
        self.assertEqual(row["is_known"], 1)

    def test_person_row_is_created_with_first_seen(self):
        self.arrive(BASE)
        person = db.get_person(self.conn, "p_10023")
        self.assertEqual(person["first_seen"], BASE)
        self.assertEqual(person["last_seen"], BASE)
        self.assertEqual(person["visit_count"], 1)

    def test_new_visit_stays_pending_for_offline_retry(self):
        decision = self.arrive(BASE)
        pending = [row["visit_id"] for row in db.pending_reports(self.conn)]
        self.assertEqual(pending, [decision.visit_id])

    def test_home_state_override_is_recorded(self):
        decision = self.book.arrive(face_event(frame_ts=BASE), home_state="occupied")
        self.assertEqual(db.get_visit(self.conn, decision.visit_id)["home_state"],
                         "occupied")

    def test_confidence_and_known_flag_are_recorded(self):
        decision = self.book.arrive(face_event(frame_ts=BASE, confidence=0.876))
        row = db.get_visit(self.conn, decision.visit_id)
        self.assertAlmostEqual(row["confidence"], 0.876)
        self.assertEqual(row["is_known"], 1)

    def test_event_is_logged_against_the_visit(self):
        decision = self.arrive(BASE, msg_id="m-abc")
        events = db.events_for_visit(self.conn, decision.visit_id)
        self.assertEqual([e["msg_id"] for e in events], ["m-abc"])
        self.assertEqual(events[0]["face_id"], "p_10023")


class TestWindow(VisitBookTestCase):
    def test_same_person_inside_window_reuses_the_visit(self):
        first = self.arrive(BASE)
        second = self.arrive(BASE + 5 * 60 * 1000)
        self.assertFalse(second.created)
        self.assertEqual(second.reason, visits.REASON_ACTIVE)
        self.assertEqual(second.visit_id, first.visit_id)
        self.assertEqual(second.person_visit_count, 1)
        self.assertEqual(len(db.pending_reports(self.conn)), 1)

    def test_boundary_599999ms_is_the_same_visit(self):
        first = self.arrive(BASE)
        # 先收口，逼业务层走窗口判据而不是"活跃到访"判据
        db.finish_visit(self.conn, first.visit_id, departed_at=BASE + 60000)
        second = self.arrive(BASE + WINDOW - 1)
        self.assertFalse(second.created)
        self.assertEqual(second.reason, visits.REASON_WINDOW)
        self.assertEqual(second.visit_id, first.visit_id)

    def test_boundary_600000ms_creates_a_new_visit(self):
        first = self.arrive(BASE)
        db.finish_visit(self.conn, first.visit_id, departed_at=BASE + 60000)
        second = self.arrive(BASE + WINDOW)
        self.assertTrue(second.created)
        self.assertEqual(second.reason, visits.REASON_NEW)
        self.assertNotEqual(second.visit_id, first.visit_id)
        self.assertEqual(second.person_visit_count, 2)

    def test_window_is_anchored_at_arrival_and_never_slides(self):
        """0 与 9 分钟算同一次；收口后第 15 分钟必须另建 —— 不能从"上一条事件"重新计。"""
        first = self.arrive(BASE)
        second = self.arrive(BASE + 9 * 60 * 1000)
        self.assertEqual(second.visit_id, first.visit_id)
        db.finish_visit(self.conn, first.visit_id, departed_at=BASE + 9 * 60 * 1000,
                        completed=True)
        third = self.arrive(BASE + 15 * 60 * 1000)
        self.assertTrue(third.created)
        self.assertNotEqual(third.visit_id, first.visit_id)
        self.assertEqual(third.person_visit_count, 2)

    def test_active_long_stay_is_not_split_by_the_clock(self):
        """站了 20 分钟的人再来一条事件：还是同一次到访（合同明文要求）。"""
        first = self.arrive(BASE)
        second = self.arrive(BASE + 20 * 60 * 1000)
        self.assertFalse(second.created)
        self.assertEqual(second.reason, visits.REASON_ACTIVE)
        self.assertEqual(second.visit_id, first.visit_id)

    def test_finished_visit_inside_window_is_reused(self):
        first = self.arrive(BASE)
        db.finish_visit(self.conn, first.visit_id, purpose="送货",
                        departed_at=BASE + 2 * 60 * 1000, completed=True)
        second = self.arrive(BASE + 9 * 60 * 1000)
        self.assertFalse(second.created)
        self.assertEqual(second.reason, visits.REASON_WINDOW)
        self.assertEqual(second.visit_id, first.visit_id)

    def test_finished_visit_is_new_visit_on_next_day(self):
        first = self.arrive(BASE)
        db.finish_visit(self.conn, first.visit_id, departed_at=BASE + 60000,
                        completed=True)
        second = self.arrive(BASE + 24 * 3600 * 1000)
        self.assertTrue(second.created)
        self.assertEqual(second.person_visit_count, 2)


class TestPeopleAreNotMixed(VisitBookTestCase):
    def test_two_known_people_get_separate_visits(self):
        a = self.arrive(BASE, face_id="p_10023", name="王阿姨")
        b = self.arrive(BASE + 1000, face_id="p_10024", name="李叔叔")
        self.assertNotEqual(a.visit_id, b.visit_id)
        self.assertEqual(db.get_visit(self.conn, b.visit_id)["face_id"], "p_10024")

    def test_unknown_ids_never_merge_with_each_other(self):
        """合同：缺少关联证据时不得跨 unk_ID 套用"同人"。"""
        first = self.arrive(BASE, face_id="unk_1", is_known=False, name=None,
                            confidence=0.0)
        second = self.arrive(BASE + 1000, face_id="unk_2", is_known=False, name=None,
                             confidence=0.0)
        self.assertTrue(first.created)
        self.assertTrue(second.created)
        self.assertNotEqual(first.visit_id, second.visit_id)
        self.assertEqual(len(db.pending_reports(self.conn)), 2)

    def test_known_name_backfills_person_row(self):
        self.arrive(BASE, is_known=False, name=None, confidence=0.1)
        self.arrive(BASE + 1000, is_known=True, name="王阿姨")
        self.assertEqual(db.get_person(self.conn, "p_10023")["name"], "王阿姨")


class TestIdempotency(VisitBookTestCase):
    def test_replayed_msg_id_is_a_noop(self):
        first = self.arrive(BASE, msg_id="m-dup")
        replay = self.arrive(BASE + 1000, msg_id="m-dup")
        self.assertFalse(replay.created)
        self.assertEqual(replay.reason, visits.REASON_REPLAY)
        self.assertEqual(replay.visit_id, first.visit_id)
        self.assertEqual(replay.person_visit_count, 1, "重传不许累加次数")
        self.assertEqual(len(db.pending_reports(self.conn)), 1)
        self.assertEqual(len(db.events_for_visit(self.conn, first.visit_id)), 1)

    def test_replay_with_a_different_face_id_is_a_protocol_error(self):
        """同一个 msg_id 又挂在别人身上：账本已被污染，不能当成重传悄悄放过。"""
        self.arrive(BASE, face_id="p_10023", msg_id="m-shared")
        with self.assertRaises(ValueError):
            self.arrive(BASE + 1000, face_id="p_10024", msg_id="m-shared")

    def test_replay_is_detected_after_restart(self):
        """幂等账本在库里（visit_event），进程重启后同一条重传也不能再来一次。"""
        first = self.arrive(BASE, msg_id="m-restart")
        self.conn.close()
        self.conn = db.connect(self.db_path)
        book = visits.VisitBook(self.conn)
        replay = book.arrive(face_event(frame_ts=BASE + 500, msg_id="m-restart"))
        self.assertFalse(replay.created)
        self.assertEqual(replay.visit_id, first.visit_id)
        self.assertEqual(replay.person_visit_count, 1)


class TestValidation(unittest.TestCase):
    def test_missing_field_names_itself_in_the_error(self):
        msg = face_event()
        del msg["frame_ts"]
        with self.assertRaises(ValueError) as ctx:
            visits.parse_face_event(msg)
        self.assertIn("frame_ts", str(ctx.exception))

    def test_wrong_type_is_rejected(self):
        msg = face_event()
        msg["type"] = "vision_heartbeat"
        with self.assertRaises(ValueError):
            visits.parse_face_event(msg)

    def test_known_without_name_is_rejected(self):
        with self.assertRaises(ValueError):
            visits.parse_face_event(face_event(is_known=True, name=None))

    def test_non_bool_is_known_is_rejected(self):
        """协议是 bool；传 0/1 会让"已知人脸没名字"这类非法组合漏过校验。"""
        msg = face_event()
        msg["is_known"] = 1
        with self.assertRaises(ValueError):
            visits.parse_face_event(msg)

    def test_non_dict_is_rejected(self):
        with self.assertRaises(ValueError):
            visits.parse_face_event("not-a-message")

    def test_negative_window_is_rejected(self):
        conn = db.connect(":memory:")
        try:
            db.init_schema(conn)
            with self.assertRaises(ValueError):
                visits.VisitBook(conn, window_ms=0)
        finally:
            conn.close()


if __name__ == "__main__":
    unittest.main()