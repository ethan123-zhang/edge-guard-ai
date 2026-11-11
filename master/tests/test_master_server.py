"""主人端进程级验收：HTTP 鉴权 / 视觉 WS 入站 / 照片上传 / 看板数据。

真实起两个监听（端口 0 = 临时端口），走的是生产代码路径：
`_Handler` → `api.Router` → `reception` → `outbox` → `sqlite`。
"""
from __future__ import annotations

import copy
import json
import os
import socket
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request

from master import api, config as master_config, push, server, store


def _http(port, method, path, body=None, token=None, headers=None):
    url = "http://127.0.0.1:%d%s" % (port, path)
    data = None
    if body is not None:
        data = body if isinstance(body, bytes) else json.dumps(body).encode("utf-8")
    request = urllib.request.Request(url, data=data, method=method)
    if token:
        request.add_header("Authorization", "Bearer %s" % token)
    for key, value in (headers or {}).items():
        request.add_header(key, value)
    try:
        with urllib.request.urlopen(request, timeout=5) as response:
            return response.status, response.read(), response.headers
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read(), exc.headers


def ws_send_event(port, payload):
    """用生产视觉客户端的协议发一条事件（8765 与 8766 同一套帧）。"""
    from vision import ws_protocol

    sock = socket.create_connection(("127.0.0.1", port), timeout=5)
    try:
        request, key = ws_protocol.client_handshake_request("127.0.0.1", port, "/")
        sock.sendall(request)
        start_line, headers = ws_protocol.read_http_head(sock)
        ws_protocol.verify_server_response(start_line, headers, key)
        sock.sendall(ws_protocol.encode_frame(json.dumps(payload), ws_protocol.OP_TEXT,
                                              mask=True))
    finally:
        sock.close()


class WsListener(object):
    """测试用主人端连接：连 `/ws?token=` 并把收到的协议消息收集起来。"""

    def __init__(self, port, token):
        from urllib.parse import quote
        from vision import ws_protocol

        self._ws = ws_protocol
        self.sock = socket.create_connection(("127.0.0.1", port), timeout=5)
        request, key = ws_protocol.client_handshake_request(
            "127.0.0.1", port, "/ws?token=" + quote(token))
        self.sock.sendall(request)
        start_line, headers = ws_protocol.read_http_head(self.sock)
        ws_protocol.verify_server_response(start_line, headers, key)
        self.sock.settimeout(0.2)
        self.messages = []
        self._stop = False
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def _run(self):
        while not self._stop:
            try:
                opcode, payload = self._ws.recv_frame(self.sock)
            except socket.timeout:
                continue
            except Exception:
                break
            if opcode == self._ws.OP_TEXT:
                try:
                    self.messages.append(json.loads(payload.decode("utf-8")))
                except ValueError:
                    pass

    def wait_for(self, msg_type, timeout=3.0):
        deadline = time.time() + timeout
        while time.time() < deadline:
            for message in list(self.messages):
                if message.get("type") == msg_type:
                    return message
            time.sleep(0.05)
        return None

    def close(self):
        self._stop = True
        try:
            self.sock.close()
        except OSError:
            pass


class ServerTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls._tmp = tempfile.TemporaryDirectory()
        cfg = copy.deepcopy(master_config.DEFAULTS)
        cfg["paths"]["db"] = os.path.join(cls._tmp.name, "sentinel.db")
        cfg["paths"]["snapshots"] = os.path.join(cls._tmp.name, "snapshots")
        cfg["security"]["vision_key"] = "vision-secret"
        cfg["security"]["token_salt"] = "salt"
        cfg["security"]["photo_secret"] = "photo-secret"
        cfg["outbox"]["poll_interval_ms"] = 100
        cls.cfg = cfg
        cls.app = server.App(cfg)
        cls.owner = server.OwnerServer(("127.0.0.1", 0), cls.app)
        cls.owner_port = cls.owner.server_address[1]
        cls.vision = server.VisionIngest(cls.app, "127.0.0.1", 0)
        cls.vision_port = cls.vision.port
        threading.Thread(target=cls.owner.serve_forever, daemon=True).start()
        cls.vision.start()
        cls.app.start_worker()
        cls.token = server.issue_token(cfg, "phone-test", "测试手机")

    @classmethod
    def tearDownClass(cls):
        cls.app._stop.set()
        cls.owner.shutdown()
        cls.owner.server_close()
        cls.vision.stop()
        cls.app.stop()
        cls._tmp.cleanup()

    # ---------------------------------------------------------------- 基础

    def test_health_is_public(self):
        status, body, _ = _http(self.owner_port, "GET", "/api/health")
        self.assertEqual(status, 200)
        payload = json.loads(body)
        self.assertTrue(payload["ok"])

    def test_health_can_require_token_for_public_entry(self):
        """公网入口下 health 也要 token：避免网址泄露就暴露"家里有没有人"。"""
        self.app.cfg["security"]["health_requires_token"] = True
        try:
            status, _, _ = _http(self.owner_port, "GET", "/api/health")
            self.assertEqual(status, 401)
            status, body, _ = _http(self.owner_port, "GET", "/api/health",
                                    token=self.token)
            self.assertEqual(status, 200)
            self.assertTrue(json.loads(body)["ok"])
        finally:
            self.app.cfg["security"]["health_requires_token"] = False

    def test_private_api_requires_token(self):
        status, _, _ = _http(self.owner_port, "GET", "/api/current")
        self.assertEqual(status, 401)
        status, _, _ = _http(self.owner_port, "GET", "/api/current",
                             token="wrong-token")
        self.assertEqual(status, 401)

    def test_current_with_token(self):
        status, body, _ = _http(self.owner_port, "GET", "/api/current",
                                token=self.token)
        self.assertEqual(status, 200)
        self.assertTrue(json.loads(body)["ok"])

    def test_unknown_route_404(self):
        status, _, _ = _http(self.owner_port, "GET", "/api/nope", token=self.token)
        self.assertEqual(status, 404)

    def test_static_dashboard_served(self):
        status, body, headers = _http(self.owner_port, "GET", "/")
        self.assertEqual(status, 200)
        self.assertIn(b"<html", body.lower())
        self.assertIn("text/html", headers.get("Content-Type", ""))

    # ---------------------------------------------------------------- 主链路

    def test_full_visit_flow(self):
        listener = WsListener(self.owner_port, self.token)
        self.addCleanup(listener.close)
        now = int(time.time() * 1000)
        face = {"type": "face_event", "msg_id": "evt-full-1", "face_id": "unk_full_1",
                "is_known": False, "name": None, "confidence": 0.93,
                "frame_ts": now}
        ws_send_event(self.vision_port, face)
        for _ in range(50):
            status, body, _ = _http(self.owner_port, "GET", "/api/current",
                                    token=self.token)
            current = json.loads(body)["current_visit"]
            if current:
                break
            time.sleep(0.1)
        self.assertIsNotNone(current)
        visit_id = current["visit_id"]

        # 照片：先传事件再传图（事件里的 msg_id 就是照片的键）
        photo = b"\xff\xd8\xff\xe0fake-jpeg-body"
        status, body, _ = _http(
            self.owner_port, "POST",
            "/vision/snapshot?msg_id=%s&face_id=unk_full_1&frame_ts=%d" % ("evt-full-1", now),
            body=photo, headers={"X-Vision-Key": "vision-secret"})
        self.assertEqual(status, 200)
        self.assertTrue(json.loads(body)["ok"])
        # 缺密钥被拒
        status, _, _ = _http(self.owner_port, "POST",
                             "/vision/snapshot?msg_id=x&face_id=y&frame_ts=1",
                             body=b"z")
        self.assertEqual(status, 401)

        # 送快递 → 收尾 → 汇总只发一次
        status, body, _ = _http(self.owner_port, "POST", "/api/utterance",
                                {"visit_id": visit_id, "text": "我送快递的，放门口就行",
                                 "utterance_id": "utt-1"}, token=self.token)
        self.assertEqual(status, 200)
        result = json.loads(body)
        self.assertEqual(result["purpose"], "送货")
        self.assertFalse(result["closed"])

        status, body, _ = _http(self.owner_port, "POST", "/api/utterance",
                                {"visit_id": visit_id, "text": "谢谢，我走了",
                                 "utterance_id": "utt-2"}, token=self.token)
        self.assertTrue(json.loads(body)["closed"])
        report = listener.wait_for("visit_report")
        self.assertIsNotNone(report)
        self.assertEqual(report["visit_id"], visit_id)
        self.assertEqual(report["dialog"]["purpose"], "送货")

        # 详情：对话全文 + 照片 + outbox=已送达
        status, body, _ = _http(self.owner_port, "GET", "/api/visits/%s" % visit_id,
                                token=self.token)
        detail = json.loads(body)
        self.assertEqual(detail["purpose"], "送货")
        self.assertEqual(len(detail["turns"]), 5)  # 问候 + 两轮问答
        self.assertIsNotNone(detail["photo"])
        outbox = [row for row in detail["outbox"] if row["kind"] == "visit_report"]
        self.assertEqual(len(outbox), 1)
        self.assertEqual(outbox[0]["status"], store.STATUS_SENT)

        # 照片可读，未授权不可读
        status, data, headers = _http(self.owner_port, "GET",
                                      "/api/visits/%s/photo" % visit_id,
                                      token=self.token)
        self.assertEqual(status, 200)
        self.assertEqual(data, photo)
        self.assertEqual(headers.get("Content-Type"), "image/jpeg")
        status, _, _ = _http(self.owner_port, "GET",
                             "/api/visits/%s/photo" % visit_id)
        self.assertEqual(status, 401)

        # 历史分页
        status, body, _ = _http(self.owner_port, "GET", "/api/visits?limit=10",
                                token=self.token)
        visits = json.loads(body)["visits"]
        self.assertTrue(any(item["visit_id"] == visit_id for item in visits))

    def test_replay_face_event_is_idempotent(self):
        now = int(time.time() * 1000)
        face = {"type": "face_event", "msg_id": "evt-replay-1", "face_id": "unk_rep_1",
                "is_known": False, "name": None, "confidence": 0.8, "frame_ts": now}
        ws_send_event(self.vision_port, face)
        ws_send_event(self.vision_port, face)
        time.sleep(0.3)
        rows = self.app.conn.execute(
            "SELECT COUNT(*) FROM visit_event WHERE msg_id='evt-replay-1'").fetchone()
        self.assertEqual(int(rows[0]), 1)

    def test_utterance_without_visit_is_400(self):
        status, _, _ = _http(self.owner_port, "POST", "/api/utterance",
                             {"visit_id": "v_nope", "text": "你好",
                              "utterance_id": "u1"}, token=self.token)
        self.assertEqual(status, 400)

    def test_ack_records_client_shown(self):
        status, body, _ = _http(self.owner_port, "POST", "/api/ack",
                                {"message_key": "report:v_x"}, token=self.token)
        self.assertEqual(status, 200)
        row = self.app.conn.execute(
            "SELECT COUNT(*) FROM event_log WHERE category='client_shown'").fetchone()
        self.assertGreaterEqual(int(row[0]), 1)

    def test_photo_signature_auth(self):
        now = int(time.time() * 1000)
        face = {"type": "face_event", "msg_id": "evt-sig-1", "face_id": "unk_sig_1",
                "is_known": False, "name": None, "confidence": 0.8, "frame_ts": now}
        ws_send_event(self.vision_port, face)
        time.sleep(0.3)
        visit = self.app.conn.execute(
            "SELECT visit_id FROM visit_event WHERE msg_id='evt-sig-1'").fetchone()
        visit_id = str(visit["visit_id"])
        photo = b"\xff\xd8signed"
        _http(self.owner_port, "POST",
              "/vision/snapshot?msg_id=evt-sig-1&face_id=unk_sig_1&frame_ts=%d" % now,
              body=photo, headers={"X-Vision-Key": "vision-secret"})
        from master import auth
        query = auth.signed_photo_query("photo-secret", visit_id, "evt-sig-1", 600)
        status, data, _ = _http(self.owner_port, "GET",
                                "/api/visits/%s/photo?%s" % (visit_id, query))
        self.assertEqual(status, 200)
        self.assertEqual(data, photo)
        status, _, _ = _http(self.owner_port, "GET",
                             "/api/visits/%s/photo?exp=1&sig=bad" % visit_id)
        self.assertEqual(status, 401)


if __name__ == "__main__":
    unittest.main()