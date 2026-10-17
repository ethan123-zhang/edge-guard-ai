"""数据库层测试（docs/02 §6 表结构）。

这一层是断网补传（`report_status` pending → sent）的载体，
也是熟人记忆（`visit_count` / `first_seen`）的唯一真源，写错一处现场就露馅。
"""
import sqlite3
import tempfile
import unittest
from pathlib import Path

from dialog import db


class DbTestCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.db_path = Path(self._tmp.name) / "sentinel.db"
        self.conn = db.connect(self.db_path)
        db.init_schema(self.conn)

    def tearDown(self):
        self.conn.close()
        self._tmp.cleanup()


class TestSchema(DbTestCase):
    def test_all_five_tables_are_created(self):
        names = {
            r["name"]
            for r in self.conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        }
        for expected in ("person", "visit", "dialog_turn", "event_log", "face_registry"):
            self.assertIn(expected, names)

    def test_init_schema_is_idempotent(self):
        db.init_schema(self.conn)  # 不能因为已存在就炸
        self.assertTrue(True)


class TestPerson(DbTestCase):
    def test_unknown_person_is_created_on_first_visit(self):
        row = db.upsert_person(self.conn, face_id="unk_1", is_known=False, now_ms=1000)
        self.assertEqual(row["visit_count"], 0)
        self.assertEqual(row["first_seen"], 1000)

    def test_upsert_never_touches_visit_count(self):
        # 唯一一次累加必须发生在 count_visit —— 两个函数都累加就是双重计数，
        # 而"第 N 次来"是答辩要讲的数字，错一次就说不圆。
        db.upsert_person(self.conn, face_id="p1", is_known=True, name="张阿姨", now_ms=1000)
        self.assertEqual(db.get_person(self.conn, "p1")["visit_count"], 0)
        db.upsert_person(self.conn, face_id="p1", is_known=True, now_ms=2000)
        self.assertEqual(db.get_person(self.conn, "p1")["visit_count"], 0)

    def test_count_visit_returns_total_including_this_one(self):
        # docs/02 §4.1：visit_count 是"含本次"的累计值，第 1 次来时值为 1
        db.upsert_person(self.conn, face_id="p1", is_known=True, name="张阿姨", now_ms=1000)
        self.assertEqual(db.count_visit(self.conn, "p1"), 1)
        self.assertEqual(db.count_visit(self.conn, "p1"), 2)

    def test_repeat_visit_keeps_first_seen_and_updates_last_seen(self):
        db.upsert_person(self.conn, face_id="p1", is_known=True, name="张阿姨", now_ms=1000)
        row = db.upsert_person(self.conn, face_id="p1", is_known=True, now_ms=2000)
        self.assertEqual(row["first_seen"], 1000)
        self.assertEqual(row["last_seen"], 2000)
        self.assertEqual(row["name"], "张阿姨")  # 名字不该被 None 覆盖掉


class TestVisit(DbTestCase):
    def setUp(self):
        super().setUp()
        db.upsert_person(self.conn, face_id="p1", is_known=True, name="张阿姨", now_ms=1000)

    def _new_visit(self, **kw):
        params = dict(
            face_id="p1", arrived_at=1000, home_state="empty", is_known=True,
            confidence=0.92, now_ms=1000,
        )
        params.update(kw)
        return db.create_visit(self.conn, **params)

    def test_visit_id_follows_the_day_sequence_format(self):
        vid = self._new_visit()
        self.assertRegex(vid, r"^v_\d{8}_\d{4}$")

    def test_second_visit_same_day_gets_next_sequence(self):
        v1 = self._new_visit()
        v2 = self._new_visit()
        self.assertNotEqual(v1, v2)
        self.assertEqual(int(v2[-4:]), int(v1[-4:]) + 1)

    def test_new_visit_is_pending_for_offline_retry(self):
        vid = self._new_visit()
        self.assertEqual(db.get_visit(self.conn, vid)["report_status"], "pending")

    def test_mark_sent_removes_it_from_pending_list(self):
        vid = self._new_visit()
        db.mark_report_sent(self.conn, vid)
        self.assertEqual(db.pending_reports(self.conn), [])

    def test_pending_reports_survive_restart(self):
        vid = self._new_visit()
        self.conn.commit()
        conn2 = db.connect(self.db_path)
        try:
            self.assertEqual([v["visit_id"] for v in db.pending_reports(conn2)], [vid])
        finally:
            conn2.close()

    def test_finish_visit_records_slots_and_completion(self):
        vid = self._new_visit()
        db.finish_visit(
            self.conn, vid, purpose="送货", caller_name="李先生",
            summary="送东西", rounds=2, completed=True,
        )
        row = db.get_visit(self.conn, vid)
        self.assertEqual(row["purpose"], "送货")
        self.assertEqual(row["caller_name"], "李先生")
        self.assertEqual(row["dialog_rounds"], 2)
        self.assertEqual(row["completed"], 1)

    def test_privileged_flag_is_persisted(self):
        vid = self._new_visit()
        db.mark_privileged(self.conn, vid, phrases=["把门打开"])
        self.assertEqual(db.get_visit(self.conn, vid)["privileged"], 1)


class TestDialogTurn(DbTestCase):
    def setUp(self):
        super().setUp()
        db.upsert_person(self.conn, face_id="p1", is_known=False, now_ms=1000)
        self.vid = db.create_visit(
            self.conn, face_id="p1", arrived_at=1000, home_state="empty",
            is_known=False, confidence=0.5, now_ms=1000,
        )

    def test_turns_are_returned_in_chronological_order(self):
        db.add_turn(self.conn, self.vid, role="assistant", text="您好", ts=1000)
        db.add_turn(self.conn, self.vid, role="user", text="我找老王", ts=2000)
        db.add_turn(self.conn, self.vid, role="assistant", text="好的", ts=3000)
        turns = db.turns_for(self.conn, self.vid)
        self.assertEqual([t["role"] for t in turns], ["assistant", "user", "assistant"])
        self.assertEqual(turns[1]["text"], "我找老王")


class TestEventLog(DbTestCase):
    def test_log_entry_is_readable_back(self):
        db.log_event(self.conn, level="warn", category="dialog", message="越权请求")
        rows = db.recent_events(self.conn)
        self.assertEqual(rows[0]["message"], "越权请求")
        self.assertEqual(rows[0]["category"], "dialog")


class TestConnection(unittest.TestCase):
    def test_rows_are_accessible_by_column_name(self):
        with tempfile.TemporaryDirectory() as tmp:
            conn = db.connect(Path(tmp) / "x.db")
            try:
                self.assertEqual(conn.execute("SELECT 1 AS answer").fetchone()["answer"], 1)
            finally:
                conn.close()

    def test_row_factory_survives_unknown_column(self):
        conn = db.connect(Path(tempfile.mkdtemp()) / "y.db")
        try:
            with self.assertRaises(sqlite3.OperationalError):
                conn.execute("SELECT * FROM nope").fetchall()
        finally:
            conn.close()


if __name__ == "__main__":
    unittest.main()
