"""主人端 PC 先行验收（设计 §10 / 任务书阶段 2–3）。

在**临时库 + 临时端口**上起真实主人端进程（`master.server.App`），用生产协议喂事件：

    py -3.12 scripts/master_sim.py --out docs/data/owner-client-2026-11-11.json

覆盖的设计验收项：
1. 注册 A / 未注册 B 顺次进场 → 各一条到访、各一张正确照片（照片乱序先到也对得上）
2. 重放 msg_id、重启服务再重放 → 不新增到访、不重复迎宾、不覆盖照片
3. 旧版无照片事件 → 记录可打开、明确缺图（不拿别的照片顶替）
4. 四行行为矩阵 + 复合句 + "别开门，我来送货"（否定不算越权）
5. 图片没到时要求开门 → 告警仍即时到手机（不等图片）
6. 断网时消息留队列、重连后收敛一条（三种观测点分别落日志）

注意：证据里所有照片都是假 JPEG 字节（不是真人素材），真机验收要重跑。
"""
from __future__ import annotations

import argparse
import copy
import json
import os
import socket
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from master import auth, config as master_config, store  # noqa: E402
from master import server as master_server  # noqa: E402
from vision import ws_protocol  # noqa: E402

PHOTO_A = b"\xff\xd8\xff\xe0photo-A-%d" % int(time.time())

CHECKS = []


def _jsonable(value):
    """证据入库时 bytes 不能直接序列化：换成可读的摘要。"""
    if isinstance(value, (bytes, bytearray)):
        return "<%d bytes>" % len(value)
    if isinstance(value, (list, tuple)):
        return [_jsonable(item) for item in value]
    if isinstance(value, dict):
        return {key: _jsonable(item) for key, item in value.items()}
    return value


def check(name, expected, actual, passed=None):
    if passed is None:
        passed = expected == actual
    CHECKS.append({"check": name, "expected": _jsonable(expected),
                   "actual": _jsonable(actual), "pass": bool(passed)})
    print("[%s] %s" % ("PASS" if passed else "FAIL", name), flush=True)
    return passed


class WsListener(object):
    def __init__(self, port, token):
        self.sock = socket.create_connection(("127.0.0.1", port), timeout=5)
        path = "/ws?token=" + urllib.parse.quote(token)
        request, key = ws_protocol.client_handshake_request("127.0.0.1", port, path)
        self.sock.sendall(request)
        start_line, headers = ws_protocol.read_http_head(self.sock)
        ws_protocol.verify_server_response(start_line, headers, key)
        self.sock.settimeout(0.2)
        self._send_lock = threading.Lock()
        self.messages = []
        self._stop = False
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def _run(self):
        while not self._stop:
            try:
                opcode, payload = ws_protocol.recv_frame(self.sock)
            except socket.timeout:
                continue
            except Exception:
                break
            if opcode == ws_protocol.OP_TEXT:
                try:
                    message = json.loads(payload.decode("utf-8"))
                except ValueError:
                    continue
                self.messages.append(message)
                # 模拟接入方确认：收到就 ack（第三种观测点另由 /api/ack 记）
                if message.get("msg_id") and message.get("type") != "ack":
                    self.ack(message["msg_id"])

    def wait_for(self, msg_type, timeout=3.0, **fields):
        deadline = time.time() + timeout
        while time.time() < deadline:
            for message in list(self.messages):
                if message.get("type") != msg_type:
                    continue
                if all(message.get(key) == value for key, value in fields.items()):
                    return message
            time.sleep(0.05)
        return None

    def ack(self, message_key):
        try:
            with self._send_lock:
                self.sock.sendall(ws_protocol.encode_frame(
                    json.dumps({"type": "ack", "message_key": message_key}),
                    ws_protocol.OP_TEXT, mask=True))
        except OSError:
            pass

    def close(self):
        self._stop = True
        try:
            self.sock.close()
        except OSError:
            pass


class Sim(object):
    """一套可重启的主人端（同一数据库可被下一个 Sim 复用）。"""

    def __init__(self, workdir):
        self.workdir = workdir
        cfg = copy.deepcopy(master_config.DEFAULTS)
        cfg["paths"]["db"] = os.path.join(workdir, "sentinel.db")
        cfg["paths"]["snapshots"] = os.path.join(workdir, "snapshots")
        cfg["security"]["vision_key"] = "sim-vision-key"
        cfg["security"]["token_salt"] = "sim-salt"
        cfg["security"]["photo_secret"] = "sim-photo-secret"
        cfg["outbox"]["poll_interval_ms"] = 100
        cfg["reception"]["silence_timeout_ms"] = 60000
        cfg["reception"]["session_timeout_ms"] = 120000
        self.cfg = cfg
        self.app = master_server.App(cfg, log=lambda message: None)
        self.owner = master_server.OwnerServer(("127.0.0.1", 0), self.app)
        self.owner_port = self.owner.server_address[1]
        self.vision = master_server.VisionIngest(self.app, "127.0.0.1", 0,
                                                 log=lambda message: None)
        self.vision_port = self.vision.port
        threading.Thread(target=self.owner.serve_forever, daemon=True).start()
        self.vision.start()
        self.app.start_worker()
        self.token = master_server.issue_token(cfg, "sim-phone", "仿真手机")
        self.listener = None
        self.base = "http://127.0.0.1:%d" % self.owner_port

    # ---------------------------------------------------------------- 基元

    def connect_client(self):
        self.listener = WsListener(self.owner_port, self.token)
        return self.listener

    def disconnect_client(self):
        if self.listener is not None:
            self.listener.close()
            self.listener = None

    def http(self, method, path, body=None, token=True, headers=None):
        url = self.base + path
        data = None
        if body is not None:
            data = body if isinstance(body, bytes) else json.dumps(body).encode("utf-8")
        request = urllib.request.Request(url, data=data, method=method)
        if token:
            request.add_header("Authorization", "Bearer " + self.token)
        for key, value in (headers or {}).items():
            request.add_header(key, value)
        try:
            with urllib.request.urlopen(request, timeout=5) as response:
                raw = response.read()
                return response.status, raw, response.headers
        except urllib.error.HTTPError as exc:
            return exc.code, exc.read(), exc.headers

    def json(self, method, path, body=None, token=True):
        status, raw, _ = self.http(method, path, body=body, token=token)
        try:
            return status, json.loads(raw)
        except ValueError:
            return status, {}

    def face(self, msg_id, face_id, frame_ts, is_known=False, name=None,
             confidence=0.9):
        payload = {"type": "face_event", "msg_id": msg_id, "face_id": face_id,
                   "is_known": is_known, "name": name, "confidence": confidence,
                   "frame_ts": int(frame_ts)}
        sock = socket.create_connection(("127.0.0.1", self.vision_port), timeout=5)
        try:
            request, key = ws_protocol.client_handshake_request(
                "127.0.0.1", self.vision_port, "/")
            sock.sendall(request)
            start_line, headers = ws_protocol.read_http_head(sock)
            ws_protocol.verify_server_response(start_line, headers, key)
            sock.sendall(ws_protocol.encode_frame(json.dumps(payload),
                                                  ws_protocol.OP_TEXT, mask=True))
        finally:
            sock.close()
        return payload

    def snapshot(self, msg_id, face_id, frame_ts, data):
        query = urllib.parse.urlencode({"msg_id": msg_id, "face_id": face_id,
                                        "frame_ts": int(frame_ts)})
        return self.http("POST", "/vision/snapshot?" + query, body=data,
                         token=False, headers={"X-Vision-Key": "sim-vision-key"})

    def utterance(self, visit_id, text, utterance_id):
        return self.json("POST", "/api/utterance",
                         {"visit_id": visit_id, "text": text,
                          "utterance_id": utterance_id})

    def current_visit(self):
        status, payload = self.json("GET", "/api/current")
        return (payload or {}).get("current_visit")

    def wait_visit(self, timeout=3.0):
        deadline = time.time() + timeout
        while time.time() < deadline:
            visit = self.current_visit()
            if visit:
                return visit
            time.sleep(0.05)
        return None

    def visit_for_event(self, msg_id, timeout=3.0):
        """等视觉事件被处理完（异步线程），返回它归到的 visit_id。"""
        deadline = time.time() + timeout
        while time.time() < deadline:
            row = self.app.conn.execute(
                "SELECT visit_id FROM visit_event WHERE msg_id=?", (msg_id,)
            ).fetchone()
            if row is not None:
                return str(row["visit_id"])
            time.sleep(0.05)
        raise AssertionError("视觉事件 %s 未被处理（超时）" % msg_id)

    def detail(self, visit_id):
        status, payload = self.json("GET", "/api/visits/" + urllib.parse.quote(visit_id))
        return status, payload

    def photo_bytes(self, visit_id):
        status, raw, _ = self.http("GET", "/api/visits/%s/photo"
                                   % urllib.parse.quote(visit_id))
        return status, raw

    def close(self):
        self.disconnect_client()
        self.app._stop.set()
        try:
            self.owner.shutdown()
        except Exception:
            pass
        self.owner.server_close()
        self.vision.stop()
        self.app.stop()


# ---------------------------------------------------------------- 场景


def scenario_ordering(tempdir):
    """A、B 顺次进场；B 的照片先到、A 的照片迟到 → 两图各归各人。"""
    sim = Sim(tempdir)
    try:
        sim.connect_client()
        now = int(time.time() * 1000)
        sim.face("evt-a", "unk_a", now)
        visit_a_id = sim.visit_for_event("evt-a")
        check("A 进场建到访", True, visit_a_id is not None)
        time.sleep(0.1)

        sim.face("evt-b", "unk_b", now + 1000)
        visit_b = sim.visit_for_event("evt-b")
        check("B 独立成一次到访", True, visit_b != visit_a_id)

        # 乱序：B 的照片先到，A 的迟到
        status, _, _ = sim.snapshot("evt-b", "unk_b", now + 1000, b"\xff\xd8photo-B")
        check("B 照片上传成功", 200, status)
        status, _, _ = sim.snapshot("evt-a", "unk_a", now, b"\xff\xd8photo-A")
        check("A 照片迟到上传成功", 200, status)

        _, detail_a = sim.detail(visit_a_id)
        _, detail_b = sim.detail(visit_b)
        check("A 绑定自己的照片", "evt-a", (detail_a.get("photo") or {}).get("msg_id"))
        check("B 绑定自己的照片", "evt-b", (detail_b.get("photo") or {}).get("msg_id"))
        status_a, bytes_a = sim.photo_bytes(visit_a_id)
        status_b, bytes_b = sim.photo_bytes(visit_b)
        check("A 图片内容正确", b"\xff\xd8photo-A", bytes_a)
        check("B 图片内容正确", b"\xff\xd8photo-B", bytes_b)
        check("照片接口 200", (200, 200), (status_a, status_b))
    finally:
        sim.close()


def scenario_replay_and_restart(tempdir):
    """重放 msg_id + 重启服务再重放：不新增到访、不重复迎宾、不覆盖照片。"""
    sim = Sim(tempdir)
    try:
        now = int(time.time() * 1000)
        sim.face("evt-replay", "unk_rep", now)
        sim.visit_for_event("evt-replay")
        sim.snapshot("evt-replay", "unk_rep", now, b"\xff\xd8replay")
        time.sleep(0.2)

        sim.face("evt-replay", "unk_rep", now)          # 同一条消息重传
        visits = sim.app.conn.execute("SELECT COUNT(*) FROM visit").fetchone()[0]
        check("重放不新增到访", 1, int(visits))
        greeting = sim.app.conn.execute(
            "SELECT COUNT(*) FROM dialog_turn WHERE role='assistant'").fetchone()[0]
        check("重放不重复迎宾", 1, int(greeting))
    finally:
        sim.close()

    restarted = Sim(tempdir)
    try:
        restarted.face("evt-replay", "unk_rep", int(time.time() * 1000))
        time.sleep(0.3)
        visits = restarted.app.conn.execute("SELECT COUNT(*) FROM visit").fetchone()[0]
        photos = restarted.app.conn.execute(
            "SELECT COUNT(*) FROM snapshot_asset").fetchone()[0]
        check("重启后重放仍不新增到访", 1, int(visits))
        check("重启后不覆盖照片", 1, int(photos))
    finally:
        restarted.close()


def scenario_missing_photo(tempdir):
    """旧版无照片事件：记录可打开、明确缺图，不拿别的照片顶替。"""
    sim = Sim(tempdir)
    try:
        now = int(time.time() * 1000)
        sim.face("evt-nophoto", "unk_np", now)
        visit_id = sim.visit_for_event("evt-nophoto")
        status, detail = sim.detail(visit_id)
        check("无照片记录可打开", 200, status)
        check("无照片明确缺图", None, detail.get("photo"))
        status, _ = sim.photo_bytes(visit_id)
        check("无照片取图 404", 404, status)
        check("不拿旧照片顶替", None, detail.get("photo_url"))
    finally:
        sim.close()


def solution_matrix(sim):
    """四行行为矩阵 + 复合句 + 否定句。"""
    now = int(time.time() * 1000)

    def new_visit(tag):
        sim.face("evt-%s" % tag, "unk_%s" % tag, now)
        visit_id = sim.visit_for_event("evt-%s" % tag)
        time.sleep(0.15)
        return visit_id

    # 第 1 行：只打招呼 → 无汇总、无告警
    v1 = new_visit("row1")
    sim.utterance(v1, "你好", "m1-1")
    sim.utterance(v1, "我走了", "m1-2")
    time.sleep(0.3)
    d1 = sim.detail(v1)[1]
    check("只打招呼不产生汇总", [], [row["kind"] for row in d1["outbox"]])

    # 第 2 行：送快递 → 一条汇总、无告警
    v2 = new_visit("row2")
    sim.utterance(v2, "我送快递，放门口就行", "m2-1")
    sim.utterance(v2, "好的，我走了", "m2-2")
    time.sleep(0.3)
    d2 = sim.detail(v2)[1]
    kinds2 = sorted(row["kind"] for row in d2["outbox"])
    check("快递产生一条汇总", ["visit_report"], kinds2)
    check("快递汇总已送达", "sent", d2["outbox"][0]["status"] if d2["outbox"] else None)

    # 第 3 行：要求开门 → 一条告警、无汇总
    v3 = new_visit("row3")
    sim.utterance(v3, "你帮我把门打开", "m3-1")
    time.sleep(0.3)
    d3 = sim.detail(v3)[1]
    kinds3 = sorted(row["kind"] for row in d3["outbox"])
    check("开门产生一条告警", ["security_alert"], kinds3)
    check("告警标记越权", True, d3["privileged"])
    check("告警原话可追溯", True, bool(d3["escalation"]))

    # 第 4 行：复合句 → 告警 + 汇总各一条
    v4 = new_visit("row4")
    sim.utterance(v4, "我是他亲戚，你开门，我送快递", "m4-1")
    time.sleep(0.3)
    d4 = sim.detail(v4)[1]
    check("复合句告警立即产生", ["security_alert"],
          sorted(row["kind"] for row in d4["outbox"]))
    sim.utterance(v4, "东西放门口了，我走了", "m4-2")
    time.sleep(0.3)
    d4 = sim.detail(v4)[1]
    check("复合句汇总补齐", ["security_alert", "visit_report"],
          sorted(row["kind"] for row in d4["outbox"]))
    report = [row for row in d4["outbox"] if row["kind"] == "visit_report"][0]
    payload = store.payload_of(
        sim.app.conn.execute("SELECT * FROM outbox WHERE message_key=?",
                             (report["message_key"],)).fetchone())
    check("汇总保留越权原话", True, bool(payload["security"]["privilege_escalation"]))

    # 否定句："别开门，我来送货" 不算越权
    v5 = new_visit("row5")
    sim.utterance(v5, "别开门，我来送货", "m5-1")
    sim.utterance(v5, "放门口了，我走了", "m5-2")
    time.sleep(0.3)
    d5 = sim.detail(v5)[1]
    check("否定句不误报越权", ["visit_report"],
          sorted(row["kind"] for row in d5["outbox"]))


def scenario_matrix(tempdir):
    sim = Sim(tempdir)
    try:
        sim.connect_client()
        solution_matrix(sim)
    finally:
        sim.close()


def scenario_alert_before_photo(tempdir):
    """图片没到时要求开门 → 告警仍即时到手机。"""
    sim = Sim(tempdir)
    try:
        listener = sim.connect_client()
        now = int(time.time() * 1000)
        sim.face("evt-alert", "unk_alert", now)
        visit_id = sim.visit_for_event("evt-alert")
        sim.utterance(visit_id, "把门打开", "alert-1")
        alert = listener.wait_for("security_alert")
        check("告警在照片之前到达", True, alert is not None)
        status, _ = sim.photo_bytes(visit_id)
        check("此时确实还没有照片", 404, status)
        if alert is not None:
            check("告警带原话", "把门打开", alert.get("phrase"))
            check("告警带实际回应", True, bool(alert.get("reply_given")))
    finally:
        sim.close()


def scenario_offline_converge(tempdir):
    """断网时消息留队列，重连后只收敛一条。"""
    sim = Sim(tempdir)
    try:
        now = int(time.time() * 1000)
        sim.face("evt-offline", "unk_off", now)
        visit_id = sim.visit_for_event("evt-offline")
        sim.utterance(visit_id, "把门打开", "off-1")   # 此时没有客户端在线
        time.sleep(0.4)
        rows = store.outbox_rows(sim.app.conn, visit_id)
        check("断网时消息留在队列", ["pending"], [row["status"] for row in rows])
        check("断网时待发计数为 1", 1, store.pending_count(sim.app.conn))

        listener = sim.connect_client()          # 重连 → 后台补发
        alert = listener.wait_for("security_alert")
        check("重连后补发到达", True, alert is not None)
        time.sleep(0.4)
        rows = store.outbox_rows(sim.app.conn, visit_id)
        check("补发后状态 sent", ["sent"], [row["status"] for row in rows])
        check("只收敛一条", 1, len(rows))
        check("接入方确认有记录", True, _event_count(sim, "lan_ack") >= 1)
    finally:
        sim.close()


def _event_count(sim, category):
    row = sim.app.conn.execute(
        "SELECT COUNT(*) FROM event_log WHERE category=?", (category,)).fetchone()
    return int(row[0])


# ---------------------------------------------------------------- main


def main(argv=None):
    parser = argparse.ArgumentParser(description="主人端 PC 先行验收")
    parser.add_argument("--out", default=os.path.join(ROOT, "docs", "data",
                                                      "owner-client-2026-11-11.json"))
    args = parser.parse_args(argv)

    tempdir = tempfile.mkdtemp(prefix="master-sim-")
    started = time.time()
    scenario_ordering(os.path.join(tempdir, "order"))
    scenario_replay_and_restart(os.path.join(tempdir, "replay"))
    scenario_missing_photo(os.path.join(tempdir, "missing"))
    scenario_matrix(os.path.join(tempdir, "matrix"))
    scenario_alert_before_photo(os.path.join(tempdir, "alert"))
    scenario_offline_converge(os.path.join(tempdir, "offline"))

    passed = sum(1 for item in CHECKS if item["pass"])
    failed = len(CHECKS) - passed
    report = {
        "suite": "owner-client-2026-11-11",
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "elapsed_s": round(time.time() - started, 2),
        "summary": {"checks": len(CHECKS), "passed": passed, "failed": failed},
        "checks": CHECKS,
        "notes": [
            "运行在临时库 + 临时端口；照片是假 JPEG 字节，不是真人素材。",
            "真实 Jetson 视觉节点与手机 App 的复验在阶段 0/4 另行记录。",
        ],
    }
    os.makedirs(os.path.dirname(os.path.abspath(args.out)), exist_ok=True)
    with open(args.out, "w", encoding="utf-8") as fh:
        json.dump(report, fh, ensure_ascii=False, indent=2)
    print("\n[master_sim] %d/%d 通过，证据：%s"
          % (passed, len(CHECKS), args.out), flush=True)
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())