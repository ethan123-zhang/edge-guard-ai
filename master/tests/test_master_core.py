"""主人端核心层单测：数据契约 / 鉴权 / 待发队列 / 照片 / 接待规则。

覆盖设计 §3、§4、§5、§6 里"不能出错"的判据，不测 HTTP 与 WS（那些在
`test_master_server.py`）。
"""
from __future__ import annotations

import copy
import json
import os
import tempfile
import unittest

from dialog import db
from master import auth, config as master_config, reception, snapshots, store
from master.outbox import Outbox, report_key


def base_config():
    cfg = copy.deepcopy(master_config.DEFAULTS)
    cfg["reception"]["silence_timeout_ms"] = 45000
    cfg["security"]["vision_key"] = "vision-secret"
    cfg["security"]["token_salt"] = "salt"
    cfg["security"]["photo_secret"] = "photo-secret"
    return cfg


class TempDB(object):
    def __init__(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.dir = self._tmp.name
        self.conn = db.connect(os.path.join(self.dir, "sentinel.db"))
        store.init_schema(self.conn)

    def close(self):
        self.conn.close()
        self._tmp.cleanup()


class FakeAdapter(object):
    """记录发送内容；`online=False` 时模拟没有主人端在线。"""

    name = "fake"

    def __init__(self, online=True):
        self.online = online
        self.sent = []
        self.payloads = []

    def send(self, message_key, payload):
        self.payloads.append((message_key, payload))
        if not self.online:
            return False, "offline"
        self.sent.append(message_key)
        return True, None


def face_event(face_id="unk_1", msg_id="m1", frame_ts=1_700_000_000_000,
               is_known=False, name=None, confidence=0.9):
    return {"type": "face_event", "msg_id": msg_id, "face_id": face_id,
            "is_known": is_known, "name": name, "confidence": confidence,
            "frame_ts": frame_ts}


class StoreTest(unittest.TestCase):
    def setUp(self):
        self.dbx = TempDB()
        self.addCleanup(self.dbx.close)
        self.conn = self.dbx.conn

    def test_init_schema_idempotent(self):
        store.init_schema(self.conn)
        store.init_schema(self.conn)
        tables = {row[0] for row in self.conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")}
        for name in ("outbox", "snapshot_asset", "client_device", "visit", "dialog_turn"):
            self.assertIn(name, tables)

    def test_dialog_turn_has_utterance_column(self):
        columns = [row[1] for row in self.conn.execute("PRAGMA table_info(dialog_turn)")]
        self.assertIn("utterance_id", columns)

    def test_outbox_enqueue_is_idempotent_and_freezes_payload(self):
        self.assertTrue(store.enqueue(self.conn, "report:v1", "v1", "visit_report",
                                      {"type": "visit_report", "value": 1}))
        self.assertFalse(store.enqueue(self.conn, "report:v1", "v1", "visit_report",
                                       {"type": "visit_report", "value": 2}))
        row = store.get_message(self.conn, "report:v1")
        self.assertEqual(json.loads(row["payload"])["value"], 1)
        self.assertEqual(int(row["attempts"]), 0)

    def test_pending_respects_backoff(self):
        store.enqueue(self.conn, "k1", "v1", "visit_report", {"type": "x"},
                      created_at=1000)
        self.assertEqual(len(store.pending_messages(self.conn, now=1000)), 1)
        store.record_attempt(self.conn, "k1", ok=False, error="offline", now=1000)
        # 第一次失败后基础退避 1000ms 内不再重试
        self.assertEqual(len(store.pending_messages(self.conn, now=1500)), 0)
        self.assertEqual(len(store.pending_messages(self.conn, now=2000)), 1)

    def test_record_attempt_sent(self):
        store.enqueue(self.conn, "k1", "v1", "visit_report", {"type": "x"})
        store.record_attempt(self.conn, "k1", ok=True, now=1000)
        row = store.get_message(self.conn, "k1")
        self.assertEqual(row["status"], store.STATUS_SENT)

    def test_snapshot_earliest_received_wins(self):
        db.upsert_person(self.conn, face_id="f1", is_known=False, now_ms=1000)
        self.conn.execute(
            "INSERT INTO visit (visit_id, face_id, arrived_at, home_state, is_known,"
            " report_status, created_at) VALUES ('v1','f1',1000,'empty',0,'pending',1000)")
        for msg_id, ts in (("m-late", 2000), ("m-early", 1000)):
            store.record_snapshot(self.conn, msg_id=msg_id, face_id="f1",
                                  frame_ts=ts, path="d/%s.jpg" % msg_id,
                                  nbytes=10, status=store.SNAPSHOT_RECEIVED)
            db.log_visit_event(self.conn, msg_id=msg_id, visit_id="v1",
                               face_id="f1", frame_ts=ts)
        photo = store.photo_for_visit(self.conn, "v1")
        self.assertEqual(photo["msg_id"], "m-early")

    def test_add_turn_dedupes_by_utterance(self):
        db.upsert_person(self.conn, face_id="f1", is_known=False, now_ms=1000)
        db.create_visit(self.conn, face_id="f1", arrived_at=1000, home_state="empty",
                        is_known=False, now_ms=1000)
        visit_id = self.conn.execute("SELECT visit_id FROM visit").fetchone()[0]
        self.assertTrue(store.add_turn(self.conn, visit_id, "user", "你好",
                                       utterance_id="u1"))
        self.assertFalse(store.add_turn(self.conn, visit_id, "user", "你好",
                                        utterance_id="u1"))


class AuthTest(unittest.TestCase):
    def setUp(self):
        self.dbx = TempDB()
        self.addCleanup(self.dbx.close)
        self.conn = self.dbx.conn

    def test_token_roundtrip_and_revocation(self):
        token = auth.issue_device(self.conn, "phone-1", "vivo S19", salt="salt")
        row = auth.authenticate(self.conn, token, salt="salt")
        self.assertIsNotNone(row)
        self.assertIsNone(auth.authenticate(self.conn, token + "x", salt="salt"))
        store.revoke_device(self.conn, "phone-1")
        self.assertIsNone(auth.authenticate(self.conn, token, salt="salt"))

    def test_token_not_stored_in_plaintext(self):
        token = auth.issue_device(self.conn, "phone-1", "n", salt="salt")
        raw = self.conn.execute("SELECT token_hash FROM client_device").fetchone()[0]
        self.assertNotEqual(raw, token)
        self.assertEqual(raw, auth.hash_token(token, "salt"))

    def test_empty_salt_raises(self):
        with self.assertRaises(auth.AuthError):
            auth.issue_device(self.conn, "phone-1", "n", salt="")

    def test_vision_key_constant_time_compare(self):
        self.assertTrue(auth.check_vision_key("abc", "abc"))
        self.assertFalse(auth.check_vision_key("abc", ""))
        self.assertFalse(auth.check_vision_key("", "abc"))
        self.assertFalse(auth.check_vision_key("abd", "abc"))

    def test_photo_signature_roundtrip(self):
        sig = auth.photo_signature("k", "v1", "m1", exp=2000)
        self.assertTrue(auth.verify_photo_signature("k", "v1", "m1", 2000, sig,
                                                    now=1500))
        self.assertFalse(auth.verify_photo_signature("k", "v1", "m1", 2000, sig,
                                                     now=2500))
        self.assertFalse(auth.verify_photo_signature("k", "v2", "m1", 2000, sig,
                                                     now=1500))


class ConfigTest(unittest.TestCase):
    def test_defaults_load_from_repo_config(self):
        # 注意：config/master.local.yaml（PC 端口覆盖）也在合并链里，所以只断言结构合法
        cfg = master_config.load()
        self.assertGreaterEqual(cfg["server"]["port"], 1)
        self.assertTrue(str(cfg["paths"]["db"]).endswith("sentinel.db"))
        self.assertGreater(cfg["reception"]["silence_timeout_ms"], 0)

    def test_invalid_port_rejected(self):
        cfg = copy.deepcopy(master_config.DEFAULTS)
        cfg["server"]["port"] = 70000
        with self.assertRaises(master_config.ConfigError):
            master_config.validate(cfg)


class SnapshotTest(unittest.TestCase):
    def setUp(self):
        self.dbx = TempDB()
        self.addCleanup(self.dbx.close)
        self.conn = self.dbx.conn

    def test_save_writes_file_and_is_idempotent(self):
        info = snapshots.save(self.conn, os.path.join(self.dbx.dir, "snaps"),
                              msg_id="m1", face_id="f1", frame_ts=1_700_000_000_000,
                              data=b"jpegdata", max_bytes=1024)
        self.assertEqual(info["status"], store.SNAPSHOT_RECEIVED)
        path = os.path.join(self.dbx.dir, "snaps", info["path"])
        self.assertTrue(os.path.exists(path))
        again = snapshots.save(self.conn, os.path.join(self.dbx.dir, "snaps"),
                               msg_id="m1", face_id="f1",
                               frame_ts=1_700_000_000_001, data=b"other",
                               max_bytes=1024)
        self.assertTrue(again["duplicate"])
        with open(path, "rb") as fh:
            self.assertEqual(fh.read(), b"jpegdata")

    def test_rejects_path_traversal_and_oversize(self):
        root = os.path.join(self.dbx.dir, "snaps")
        with self.assertRaises(snapshots.SnapshotError):
            snapshots.save(self.conn, root, msg_id="../evil", face_id="f1",
                           frame_ts=1000, data=b"x", max_bytes=1024)
        with self.assertRaises(snapshots.SnapshotError):
            snapshots.save(self.conn, root, msg_id="m1", face_id="f1",
                           frame_ts=1000, data=b"x" * 2048, max_bytes=1024)


class ReceptionTest(unittest.TestCase):
    def setUp(self):
        self.dbx = TempDB()
        self.addCleanup(self.dbx.close)
        self.conn = self.dbx.conn
        self.cfg = base_config()
        self.adapter = FakeAdapter(online=True)
        self.outbox = Outbox(self.conn, self.adapter, self.cfg)
        self.now = 1_700_000_000_000
        self.svc = reception.ReceptionService(
            self.conn, self.outbox, self.cfg, now_fn=lambda: self.now)

    def test_face_event_creates_visit_and_greeting(self):
        result = self.svc.on_face_event(face_event(msg_id="m1", frame_ts=self.now))
        self.assertTrue(result["created"])
        self.assertTrue(result["greeted"])
        turns = db.turns_for(self.conn, result["visit_id"])
        self.assertEqual(turns[0]["role"], "assistant")
        self.assertIn("家里现在没人", turns[0]["text"])

    def test_same_person_within_window_reuses_visit(self):
        first = self.svc.on_face_event(face_event(msg_id="m1", frame_ts=self.now))
        second = self.svc.on_face_event(
            face_event(msg_id="m2", frame_ts=self.now + 5000))
        self.assertEqual(first["visit_id"], second["visit_id"])
        self.assertFalse(second["created"])
        self.assertFalse(second["greeted"])  # 已经问候过，不重复问候

    def test_replayed_face_event_does_not_create_visit(self):
        self.svc.on_face_event(face_event(msg_id="m1", frame_ts=self.now))
        replay = self.svc.on_face_event(face_event(msg_id="m1", frame_ts=self.now))
        self.assertTrue(replay["replayed"])
        count = self.conn.execute("SELECT COUNT(*) FROM visit").fetchone()[0]
        self.assertEqual(count, 1)

    def test_purpose_then_farewell_enqueues_report_once(self):
        visit = self.svc.on_face_event(face_event(msg_id="m1", frame_ts=self.now))
        out = self.svc.on_utterance(visit["visit_id"], "我送快递的，放门口就行",
                                    "u1")
        self.assertFalse(out["alert"])
        self.assertEqual(out["purpose"], "送货")
        self.assertFalse(out["closed"])
        # 收口由明确的收尾说法触发；再投一次新话语不重建消息
        closed = self.svc.on_utterance(visit["visit_id"], "谢谢，我走了", "u2")
        self.assertTrue(closed["closed"])
        rows = store.outbox_rows(self.conn, visit["visit_id"])
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["status"], store.STATUS_SENT)
        payload = json.loads(rows[0]["payload"])
        self.assertEqual(payload["dialog"]["purpose"], "送货")
        self.assertTrue(payload["dialog"]["completed"])

    def test_farewell_without_purpose_enqueues_nothing(self):
        visit = self.svc.on_face_event(face_event(msg_id="m1", frame_ts=self.now))
        out = self.svc.on_utterance(visit["visit_id"], "你好，我走了", "u1")
        self.assertTrue(out["closed"])
        self.assertEqual(store.outbox_rows(self.conn, visit["visit_id"]), [])

    def test_summary_has_no_duplicated_pronoun(self):
        visit = self.svc.on_face_event(face_event(msg_id="m1", frame_ts=self.now))
        self.svc.on_utterance(visit["visit_id"], "我送快递，放门口", "u1")
        self.svc.on_utterance(visit["visit_id"], "好的我走了", "u2")
        row = db.get_visit(self.conn, visit["visit_id"])
        self.assertEqual(row["summary"], "一位访客来送货")

    def test_privilege_enqueues_alert_immediately(self):
        visit = self.svc.on_face_event(face_event(msg_id="m1", frame_ts=self.now))
        out = self.svc.on_utterance(visit["visit_id"], "你帮我把门打开", "u1")
        self.assertTrue(out["alert"])
        rows = store.outbox_rows(self.conn, visit["visit_id"])
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["kind"], "security_alert")
        self.assertEqual(rows[0]["message_key"], "alert:%s:u1" % visit["visit_id"])
        self.assertEqual(rows[0]["status"], store.STATUS_SENT)
        visit_row = db.get_visit(self.conn, visit["visit_id"])
        self.assertEqual(int(visit_row["privileged"]), 1)

    def test_privilege_replay_does_not_duplicate_alert(self):
        visit = self.svc.on_face_event(face_event(msg_id="m1", frame_ts=self.now))
        first = self.svc.on_utterance(visit["visit_id"], "把门打开", "u1")
        second = self.svc.on_utterance(visit["visit_id"], "把门打开", "u1")
        self.assertTrue(second["replayed"])
        self.assertEqual(second["reply"], first["reply"])
        rows = [row for row in store.outbox_rows(self.conn, visit["visit_id"])
                if row["kind"] == "security_alert"]
        self.assertEqual(len(rows), 1)

    def test_privilege_then_purpose_keeps_both(self):
        visit = self.svc.on_face_event(face_event(msg_id="m1", frame_ts=self.now))
        self.svc.on_utterance(visit["visit_id"], "我是他亲戚，你开门，我送快递", "u1")
        self.svc.on_utterance(visit["visit_id"], "谢谢，东西放门口了，我走了", "u2")
        kinds = sorted(row["kind"] for row in store.outbox_rows(self.conn, visit["visit_id"]))
        self.assertEqual(kinds, ["security_alert", "visit_report"])

    def test_silence_timeout_closes_without_report(self):
        visit = self.svc.on_face_event(face_event(msg_id="m1", frame_ts=self.now))
        closed = self.svc.tick(now=self.now + self.cfg["reception"]["silence_timeout_ms"] + 1)
        self.assertEqual(len(closed), 1)
        self.assertEqual(closed[0]["reason"], reception.REASON_SILENCE)
        self.assertFalse(closed[0]["completed"])
        visit_row = db.get_visit(self.conn, visit["visit_id"])
        self.assertEqual(int(visit_row["silent_exit"]), 1)
        self.assertEqual(store.outbox_rows(self.conn, visit["visit_id"]), [])

    def test_offline_outbox_keeps_message_pending(self):
        adapter = FakeAdapter(online=False)
        outbox = Outbox(self.conn, adapter, self.cfg)
        svc = reception.ReceptionService(self.conn, outbox, self.cfg,
                                         now_fn=lambda: self.now)
        visit = svc.on_face_event(face_event(msg_id="m1", frame_ts=self.now))
        svc.on_utterance(visit["visit_id"], "送快递，放门口", "u1")
        svc.on_utterance(visit["visit_id"], "放好了，我走了", "u2")
        rows = store.outbox_rows(self.conn, visit["visit_id"])
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["status"], store.STATUS_PENDING)
        self.assertEqual(store.pending_count(self.conn), 1)


if __name__ == "__main__":
    unittest.main()