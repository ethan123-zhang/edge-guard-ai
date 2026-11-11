"""主人端进程：单端口 8766（HTTP + WebSocket 按 Upgrade 分流）+ 8765 视觉入站。

启动：
    py -3.12 -m master.server --config config/master.yaml
    py -3.12 -m master.server --issue-token phone-1 --name "vivo S19"   # 登记设备，打印一次 token

两条监听：
- 8766：主人端（Web 看板 / APK）的 HTTP API + WS 推送；手机只记一个地址。
- 8765：视觉节点入站（docs/02 §1 保持原样），视觉侧 `ws_url` 指向本机 8765 即可。

线程模型：请求都在 `app.lock` 里串行处理 SQLite；后台 worker 每 `poll_interval_ms`
做一次 outbox 补发 + 接待超时收口。
"""
from __future__ import annotations

import json
import signal
import socket
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Dict, Optional
from urllib.parse import parse_qs, urlparse

from dialog import db
from master import api, auth, config as master_config, push, snapshots, store
from master.outbox import Outbox
from master.reception import ReceptionService
from master.ws import ClientHub
from vision import ws_protocol


class App(object):
    """进程级共享状态（数据库、队列、接待会话、连接注册表）。"""

    def __init__(self, cfg: Dict[str, Any], log=None):
        self.cfg = cfg
        self._log = log or (lambda message: print(message, flush=True))
        self.lock = threading.RLock()
        self.conn = db.connect(master_config.db_path(cfg))
        store.init_schema(self.conn)
        self.snapshots_root = master_config.snapshots_dir(cfg)
        self.snapshots_root.mkdir(parents=True, exist_ok=True)
        self.hub = ClientHub(log=self._log)
        adapter = push.create(cfg["push"]["adapter"], cfg=cfg,
                              broadcast=self.hub.broadcast, log=self._log)
        self.outbox = Outbox(self.conn, adapter, cfg, log=self._log)
        self.reception = ReceptionService(self.conn, self.outbox, cfg,
                                          log=self._log, notify=self._notify)
        self.router = api.Router(self)
        self.last_vision_ts = int(time.time() * 1000)
        self._state_seq = 0
        self._stop = threading.Event()

    # ---------------------------------------------------------------- 状态广播

    def _notify(self, payload: Dict[str, Any]) -> None:
        try:
            self.hub.broadcast(payload)
        except Exception as exc:  # 广播失败不影响主流程
            self._log("[server] 广播失败：%s" % exc)

    def broadcast_state(self) -> None:
        payload = self.state_payload()
        self._notify(payload)

    def state_payload(self) -> Dict[str, Any]:
        with self.lock:
            self._state_seq += 1
            now = int(time.time() * 1000)
            current = self.reception_current_visit()
            local = time.localtime()
            midnight_ms = int(time.mktime((local.tm_year, local.tm_mon, local.tm_mday,
                                           0, 0, 0, 0, 0, -1)) * 1000)
            row = self.conn.execute(
                "SELECT COUNT(*) AS n FROM visit WHERE arrived_at >= ?",
                (midnight_ms,),
            ).fetchone()
            open_sessions = [vid for vid, s in self.reception.sessions.items()
                             if not s.get("closed")]
            return {
                "type": "state_sync",
                "msg_id": "state:%d:%d" % (now, self._state_seq),
                "ts": now,
                "home_state": self.reception.home_state,
                "vision_online": bool(now - int(self.last_vision_ts) < 15000),
                "dialog_busy": bool(open_sessions),
                "llm_ready": False,          # 本阶段不接 LLM（任务书边界）
                "current_visit": current,
                "today_visits": int(row["n"]) if row is not None else 0,
            }

    def reception_current_visit(self) -> Optional[Dict[str, Any]]:
        rows = self.conn.execute(
            "SELECT * FROM visit WHERE departed_at IS NULL AND completed=0"
            " ORDER BY arrived_at DESC, created_at DESC LIMIT 1"
        ).fetchall()
        if not rows:
            return None
        row = rows[0]
        person = db.get_person(self.conn, str(row["face_id"]))
        return {"visit_id": str(row["visit_id"]), "arrived_at": int(row["arrived_at"]),
                "face_id": str(row["face_id"]),
                "is_known": bool(int(row["is_known"])),
                "name": person["name"] if person is not None else None,
                "visit_count": int(person["visit_count"]) if person is not None else 0,
                "purpose": row["purpose"]}

    # ---------------------------------------------------------------- 视觉事件

    def on_vision_message(self, text: str) -> None:
        """8765 收到一条视觉消息：face_event 走接待主循环，心跳只刷新在线时间。"""
        try:
            payload = json.loads(text)
        except ValueError:
            self._log("[vision] 收到非 JSON 文本，已忽略")
            return
        if not isinstance(payload, dict):
            return
        msg_type = payload.get("type")
        with self.lock:
            self.last_vision_ts = int(time.time() * 1000)
            if msg_type == "vision_heartbeat":
                return
            if msg_type != "face_event":
                self._log("[vision] 未知消息类型 %r，忽略" % (msg_type,))
                return
            try:
                result = self.reception.on_face_event(payload)
            except ValueError as exc:
                self._log("[vision] face_event 非法：%s" % exc)
                return
        self._log("[vision] %s → %s（created=%s）"
                  % (result.get("face_id"), result.get("visit_id"),
                     result.get("created")))
        self.broadcast_state()

    # ---------------------------------------------------------------- 后台线程

    def start_worker(self) -> threading.Thread:
        interval = int(self.cfg["outbox"].get("poll_interval_ms", 500)) / 1000.0

        def loop():
            while not self._stop.is_set():
                closed = []
                try:
                    with self.lock:
                        self.outbox.flush()
                        closed = self.reception.tick()
                except Exception as exc:
                    self._log("[server] 后台任务异常：%s" % exc)
                if closed:
                    self._log("[server] 收口 %d 次接待" % len(closed))
                    self.broadcast_state()
                if self._stop.wait(interval):
                    break

        thread = threading.Thread(target=loop, name="master-worker", daemon=True)
        thread.start()
        return thread

    def stop(self) -> None:
        self._stop.set()
        self.hub.close_all()
        try:
            self.conn.close()
        except Exception:
            pass


# ---------------------------------------------------------------- HTTP/WS 服务


class _Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.0"
    server_version = "SentinelMaster/0.1"

    def log_message(self, fmt, *args):  # 不刷屏；业务日志走 app 的 log
        pass

    # -- 路由 ------------------------------------------------------------
    def do_GET(self):
        self._dispatch("GET")

    def do_POST(self):
        self._dispatch("POST")

    def do_HEAD(self):
        self._dispatch("HEAD")

    def _dispatch(self, method):
        app = self.server.app  # type: ignore[attr-defined]
        parsed = urlparse(self.path)
        query = parse_qs(parsed.query)
        if method == "GET" and parsed.path == "/ws":
            self._handle_websocket(app, query)
            return
        try:
            body = self._read_body(app)
        except ValueError as exc:
            self._send(api.error(400, str(exc)))
            return
        headers = {key.lower(): value for key, value in self.headers.items()}
        with app.lock:
            response = app.router.handle(method, parsed.path, query, headers, body)
        if method == "HEAD":
            response.body = b""
        self._send(response)

    def _read_body(self, app) -> bytes:
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            raise ValueError("Content-Length 非法")
        max_bytes = int(app.cfg["photo"]["max_bytes"]) + api.JSON_LIMIT
        if length < 0 or length > max_bytes:
            raise ValueError("请求体过大")
        if length == 0:
            return b""
        return self.rfile.read(length)

    def _send(self, response: api.Response) -> None:
        self.send_response(response.status)
        for key, value in response.headers.items():
            self.send_header(key, value)
        self.send_header("Content-Length", str(len(response.body)))
        self.end_headers()
        if response.body:
            self.wfile.write(response.body)

    # -- WebSocket -------------------------------------------------------
    def _handle_websocket(self, app, query):
        key = self.headers.get("Sec-WebSocket-Key")
        if not key:
            self._send(api.error(400, "缺少 Sec-WebSocket-Key"))
            return
        salt = app.cfg["security"].get("token_salt", "")
        token = (query.get("token") or [""])[0]
        if not salt:
            self._send(api.error(503, "服务端未配置 token_salt"))
            return
        device = auth.authenticate(app.conn, token, salt=salt)
        if device is None:
            self._send(api.error(401, "未授权：token 缺失或无效"))
            return
        accept = ws_protocol.accept_key(key)
        handshake = (
            "HTTP/1.1 101 Switching Protocols\r\n"
            "Upgrade: websocket\r\n"
            "Connection: Upgrade\r\n"
            "Sec-WebSocket-Accept: %s\r\n\r\n" % accept
        )
        self.wfile.write(handshake.encode("latin-1"))
        self.wfile.flush()

        client = app.hub.register(self.connection, str(device["device_id"]),
                                  self.client_address)
        try:
            while not app._stop.is_set():
                try:
                    opcode, payload = ws_protocol.recv_frame(self.connection)
                except socket.timeout:
                    continue
                except (ws_protocol.WsError, OSError):
                    break
                if opcode == ws_protocol.OP_CLOSE:
                    break
                if opcode == ws_protocol.OP_PING:
                    try:
                        self.connection.sendall(
                            ws_protocol.encode_frame(payload, ws_protocol.OP_PONG))
                    except OSError:
                        break
                    continue
                if opcode != ws_protocol.OP_TEXT:
                    continue
                self._on_client_text(app, client, payload.decode("utf-8", "replace"))
        finally:
            app.hub.unregister(client)
        self.close_connection = True

    def _on_client_text(self, app, client, text: str) -> None:
        try:
            message = json.loads(text)
        except ValueError:
            return
        if not isinstance(message, dict):
            return
        msg_type = message.get("type")
        if msg_type == "ack":
            key = str(message.get("message_key") or "")
            if key:
                with app.lock:
                    app.outbox.note_lan_ack(key, client.device_id)
            return
        if msg_type == "history_request":
            payload = self._history_payload(app, message)
            client.send_json(payload)
            return
        if msg_type == "set_home_state":
            state = str(message.get("home_state") or "")
            if state not in ("home", "away", "empty", "occupied"):
                state = "empty"
            with app.lock:
                app.reception.set_home_state(state)
            app.broadcast_state()
            return
        # 未知类型忽略不报错（docs/02 §8）

    def _history_payload(self, app, message) -> Dict[str, Any]:
        request_id = str(message.get("request_id") or "")
        limit = 20
        try:
            rows = app.conn.execute(
                "SELECT * FROM visit ORDER BY arrived_at DESC LIMIT ?", (limit,)
            ).fetchall()
            visits = []
            for row in rows:
                person = db.get_person(app.conn, str(row["face_id"]))
                visits.append({
                    "visit_id": str(row["visit_id"]),
                    "arrived_at": int(row["arrived_at"]),
                    "duration_ms": int(row["duration_ms"]) if row["duration_ms"] else None,
                    "is_known": bool(int(row["is_known"])),
                    "name": person["name"] if person is not None else None,
                    "visit_count": int(person["visit_count"]) if person is not None else 0,
                    "summary": row["summary"],
                    "purpose": row["purpose"],
                    "privilege_escalation": bool(int(row["privileged"] or 0)),
                })
        except Exception:
            visits = []
        return {"type": "history_response", "msg_id": "history:%d" % int(time.time() * 1000),
                "ts": int(time.time() * 1000), "request_id": request_id,
                "visits": visits, "total": len(visits)}


class OwnerServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, addr, app):
        self.app = app
        super().__init__(addr, _Handler)


# ---------------------------------------------------------------- 8765 视觉入站


class VisionIngest(object):
    """视觉节点入站：每连接一个线程，只收不发（协议 docs/02 §3）。"""

    def __init__(self, app, host: str, port: int, log=None):
        self.app = app
        self._log = log or (lambda message: None)
        self._sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._sock.bind((host, port))
        self._sock.listen(8)
        self.port = self._sock.getsockname()[1]
        self._sock.settimeout(0.5)
        self._threads = []
        self._closed = False

    def start(self) -> None:
        thread = threading.Thread(target=self._accept_loop, name="vision-accept",
                                  daemon=True)
        thread.start()

    def _accept_loop(self) -> None:
        self._log("[vision] 监听 ws://%s（视觉节点入站）" % (self.port,))
        while not self._closed:
            try:
                sock, addr = self._sock.accept()
            except socket.timeout:
                continue
            except OSError:
                break
            thread = threading.Thread(target=self._serve, args=(sock, addr),
                                      name="vision-%s" % (addr,), daemon=True)
            thread.start()
            self._threads.append(thread)

    def _serve(self, sock, addr) -> None:
        try:
            sock.settimeout(5.0)
            ws_protocol.server_handshake(sock)
            self._log("[vision] 视觉节点已连接：%s:%s" % addr)
            while not self._closed:
                try:
                    text = ws_protocol.recv_text(sock)
                except socket.timeout:
                    continue
                except (ws_protocol.WsError, OSError):
                    break
                if text is None:
                    break
                self.app.on_vision_message(text)
        except Exception as exc:
            self._log("[vision] 连接异常：%s" % exc)
        finally:
            try:
                sock.close()
            except OSError:
                pass

    def stop(self) -> None:
        self._closed = True
        try:
            self._sock.close()
        except OSError:
            pass


# ---------------------------------------------------------------- main


def issue_token(cfg, device_id: str, name: str) -> str:
    conn = db.connect(master_config.db_path(cfg))
    store.init_schema(conn)
    try:
        return auth.issue_device(conn, device_id, name,
                                 salt=cfg["security"].get("token_salt", ""))
    finally:
        conn.close()


def run(cfg: Dict[str, Any]) -> int:
    app = App(cfg)
    host = cfg["server"]["host"]
    owner = OwnerServer((host, int(cfg["server"]["port"])), app)
    vision = VisionIngest(app, host, int(cfg["server"]["vision_port"]))
    owner_thread = threading.Thread(target=owner.serve_forever, name="owner-http",
                                    daemon=True)
    owner_thread.start()
    vision.start()
    worker = app.start_worker()
    print("[master] 主人端已启动：HTTP/WS %s:%d，视觉入站 %d，库 %s"
          % (host, owner.server_address[1], vision.port, master_config.db_path(cfg)),
          flush=True)
    print("[master] 看板：http://<本机地址>:%d/" % owner.server_address[1],
          flush=True)

    def _stop(signum, frame):
        print("[master] 收到退出信号 %s" % signum, flush=True)
        owner.shutdown()

    try:
        signal.signal(signal.SIGINT, _stop)
        signal.signal(signal.SIGTERM, _stop)
    except (ValueError, OSError):
        pass

    try:
        while owner_thread.is_alive():
            owner_thread.join(0.5)
    except KeyboardInterrupt:
        pass
    finally:
        app._stop.set()
        vision.stop()
        owner.server_close()
        app.stop()
        worker.join(timeout=2)
    return 0


def main(argv=None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    config_path = None
    if "--config" in argv:
        config_path = argv[argv.index("--config") + 1]
    cfg = master_config.load(path=config_path) if config_path else master_config.load()

    if "--issue-token" in argv:
        idx = argv.index("--issue-token")
        device_id = argv[idx + 1] if idx + 1 < len(argv) else ""
        name = ""
        if "--name" in argv:
            name = argv[argv.index("--name") + 1]
        if not device_id:
            print("用法：--issue-token <device_id> [--name <显示名>]", file=sys.stderr)
            return 2
        token = issue_token(cfg, device_id, name)
        print("[master] 设备 %s 的 token（只显示这一次，请存到手机里）：" % device_id)
        print(token)
        return 0

    return run(cfg)


if __name__ == "__main__":
    raise SystemExit(main())