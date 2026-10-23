"""假对话节点：在 PC 上扮演鲁班猫5 的 8765 端口，收事件 → 写 SQLite → 打印。

用途（docs/06 W2-1/W2-2 的提前验收）：
    终端 A：python scripts\\vision_endpoint_sim.py
    终端 B：python scripts\\vision_sim.py
    终端 C：python -c "from dialog import db; ..."   或直接看 data\\sentinel.db

**这里只做"收到并存下"**：`visit` 表要等对话状态机建（下一轮），现在写的是
`person` 与 `event_log` —— 别在这里假装已经跑完了到访流程。
"""
import argparse
import json
import os
import socket
import sys
import threading
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from dialog import db          # noqa: E402  （路径注入后才能导入）
from vision import ws_protocol  # noqa: E402


def log(message):
    print("%s %s" % (time.strftime("%H:%M:%S"), message))


class EventStore(object):
    """收到的事件落到 SQLite。加锁因为每个客户端一个线程。"""

    def __init__(self, db_path, stop_event=None):
        self.conn = db.connect(db_path)
        db.init_schema(self.conn)
        self.lock = threading.Lock()
        self.counts = {"face_event": 0, "vision_heartbeat": 0}
        self.stop_event = stop_event
        self._last_hb_log = 0.0

    def record(self, msg):
        kind = msg.get("type")
        with self.lock:
            if kind == "face_event":
                self._record_face_event(msg)
            elif kind == "vision_heartbeat":
                self.counts[kind] += 1
                now = time.time()
                if now - self._last_hb_log >= 5.0:
                    self._last_hb_log = now
                    log("[心跳] fps=%.1f 丢帧=%s（累计 %d 次）"
                        % (float(msg.get("fps", 0)), msg.get("frame_drop"), self.counts[kind]))
            else:
                log("[未知类型] %s —— 按协议 §8 忽略，不报错" % kind)

    def _record_face_event(self, msg):
        problems = self._validate(msg)
        self.counts["face_event"] += 1
        is_known = bool(msg.get("is_known"))
        name = msg.get("name") if is_known else None
        db.upsert_person(self.conn, str(msg["face_id"]), is_known, db.now_ms(), name=name)
        db.log_event(
            self.conn, level="info", category="vision",
            message="收到 face_event", detail=msg,
        )
        snapshot = "（无）"
        log("[人脸] #%d %s %s conf=%.2f bbox=%s%s" % (
            self.counts["face_event"], msg.get("face_id"),
            ("[" + name + "]") if name else "[陌生人]",
            float(msg.get("confidence", 0)), msg.get("bbox"),
            ("  ⚠ " + "；".join(problems)) if problems else "",
        ))
        if self.stop_event is not None:
            self.stop_event.set()

    @staticmethod
    def _validate(msg):
        """宽容接收，但把不合规的点打印出来 —— 联调时这里是第一现场。"""
        problems = []
        for key in ("type", "msg_id", "ts", "face_id", "is_known", "name", "confidence", "frame_ts"):
            if key not in msg:
                problems.append("缺 %s" % key)
        if isinstance(msg.get("ts"), int) and msg["ts"] < 10 ** 12:
            problems.append("ts 像秒级")
        return problems


class EndpointServer(object):
    def __init__(self, host="127.0.0.1", port=8765, store=None, log_fn=log):
        self.host = host
        self.port = int(port)
        self.store = store
        self.log = log_fn
        self._sock = None

    def serve(self, duration=0.0, stop_event=None):
        stop_event = stop_event or threading.Event()
        srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        srv.bind((self.host, self.port))
        srv.listen(5)
        srv.settimeout(1.0)
        self._sock = srv
        deadline = (time.time() + float(duration)) if duration else None
        self.log("[endpoint] 监听 ws://%s:%d（Ctrl-C 退出）" % (self.host, self.port))
        try:
            while not stop_event.is_set():
                if deadline is not None and time.time() >= deadline:
                    break
                try:
                    conn, addr = srv.accept()
                except socket.timeout:
                    continue
                except OSError:
                    break
                thread = threading.Thread(target=self._serve_client, args=(conn, addr))
                thread.daemon = True
                thread.start()
        finally:
            try:
                srv.close()
            except OSError:
                pass
            self.log("[endpoint] 关闭监听")
        return self.store.counts

    def _serve_client(self, conn, addr):
        conn.settimeout(60)
        try:
            ws_protocol.server_handshake(conn)
            self.log("[endpoint] 视觉节点已连接：%s:%d" % (addr[0], addr[1]))
            while True:
                try:
                    text = ws_protocol.recv_text(conn)
                except socket.timeout:
                    continue
                if text is None:
                    break
                try:
                    msg = json.loads(text)
                except ValueError:
                    self.log("[endpoint] 收到非 JSON 文本，已忽略：%r" % text[:80])
                    continue
                self.store.record(msg)
        except (ws_protocol.WsError, OSError) as exc:
            if not isinstance(exc, socket.timeout):
                self.log("[endpoint] 客户端断开：%s" % exc)
        finally:
            try:
                conn.close()
            except OSError:
                pass
            self.log("[endpoint] 连接结束")


def main(argv=None):
    ap = argparse.ArgumentParser(description="哨兵管家 · 假对话节点（收 face_event）")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8765)
    ap.add_argument("--db", default=os.path.join(ROOT, "data", "sentinel.db"))
    ap.add_argument("--duration", type=float, default=0.0, help="跑多少秒后退出（0=一直等）")
    ap.add_argument("--once", action="store_true", help="收到第一条 face_event 就退出（自动化测试用）")
    args = ap.parse_args(argv)

    stop_event = threading.Event()
    store = EventStore(args.db, stop_event=stop_event if args.once else None)
    log("[endpoint] 数据库：%s" % args.db)
    server = EndpointServer(args.host, args.port, store=store)
    try:
        counts = server.serve(duration=args.duration, stop_event=stop_event)
    except KeyboardInterrupt:
        log("[endpoint] 收到 Ctrl-C")
        counts = store.counts
    log("[endpoint] 总计：%s" % json.dumps(counts, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
