"""WebSocket 客户端：把事件发给对话节点（docs/02 §3，端口 8765）。

行为约定（现场排障要看这个）：
- `send()` **不抛异常**，返回 True/False。视觉节点是"生产者"，网络断了不能把
  主循环拖死 —— 断开时计失败数，由 `Service` 决定重连节奏（heartbeat 里体现）。
- **所有网络等待都有界**（2026-11-07 修）：对端 accept 了但不应答时，握手读会一直
  挂着；对端不读时 `sendall` 也会挂死（TCP 窗口满）。两者都会把 30 fps 的主循环
  拖到 8 fps。现在握手用 `timeout`，发帧用 `send_timeout`，超时立刻断开。
- 重连退避：连不上不是每 `reconnect_sec` 硬刷，而是 3s → 6s → 12s … 封顶
  `max_reconnect_sec`。对端"半死"（accept 了不应答）时，每次探测仍要花 `timeout`，
  指数退避把这段损失从"每 3s 一次"降到"每 30s 一次"，帧率才不会被拖塌。
"""
import socket
import time
from urllib.parse import urlparse

from vision import ws_protocol


class WsClientError(RuntimeError):
    pass


class WebSocketClient(object):
    def __init__(self, url, timeout=1.0, reconnect_sec=3.0, send_timeout=0.5,
                 max_reconnect_sec=60.0, log=None):
        self.url = str(url)
        self.timeout = float(timeout)
        self.send_timeout = float(send_timeout)
        self.reconnect_sec = float(reconnect_sec)
        self.max_reconnect_sec = max(float(max_reconnect_sec), float(reconnect_sec))
        self.log = log or (lambda msg: None)
        self._sock = None
        self._host = None
        self._port = None
        self._path = "/"
        self._parse_url()
        self._last_attempt = 0.0
        self._fail_streak = 0
        self._next_attempt = 0.0
        self.connect_failures = 0
        self.last_error = ""

    def _parse_url(self):
        parsed = urlparse(self.url)
        if parsed.scheme not in ("ws", "wss"):
            raise WsClientError("只支持 ws://（当前 %r）" % self.url)
        if not parsed.hostname:
            raise WsClientError("URL 里没有主机名：%r" % self.url)
        self._host = parsed.hostname
        self._port = int(parsed.port or 80)
        self._path = parsed.path or "/"
        if parsed.query:
            self._path += "?" + parsed.query

    # ---------------------------------------------------------------- 连接

    @property
    def connected(self):
        return self._sock is not None

    def _note_failure(self):
        """记一次失败并算下次允许探测的时刻（指数退避，封顶 max_reconnect_sec）。"""
        self.connect_failures += 1
        self._fail_streak += 1
        delay = self.reconnect_sec * (2 ** min(self._fail_streak - 1, 16))
        delay = min(delay, self.max_reconnect_sec)
        self._next_attempt = time.time() + delay
        self._last_attempt = time.time()

    def _note_success(self):
        self._fail_streak = 0
        self._next_attempt = 0.0
        self._last_attempt = time.time()
        self.last_error = ""

    def connect(self):
        """连不上/握不上手都抛 WsClientError；调用方可以忽略并稍后重试。

        握手全程受 `timeout` 约束（不设 None）：对端 accept 之后不说话是最阴的
        一种"连上了"，没有超时就会把主循环永久卡在 send 里。
        """
        self.close()
        try:
            sock = socket.create_connection((self._host, self._port), timeout=self.timeout)
        except OSError as exc:
            self._note_failure()
            self.last_error = "连不上：%s" % exc
            raise WsClientError("连不上 %s:%d（%s）" % (self._host, self._port, exc))
        request, key = ws_protocol.client_handshake_request(self._host, self._port, self._path)
        try:
            sock.sendall(request)
            start_line, headers = ws_protocol.read_http_head(sock)
            ws_protocol.verify_server_response(start_line, headers, key)
        except (ws_protocol.WsError, OSError) as exc:
            try:
                sock.close()
            except OSError:
                pass
            self._note_failure()
            self.last_error = "握手失败：%s" % exc
            raise WsClientError("WebSocket 握手失败（%s）" % exc)
        # 握手成功后才把超时收到 send_timeout：发一条事件必须尽快失败，不能拖帧
        sock.settimeout(self.send_timeout)
        self._sock = sock
        self._note_success()
        return self

    def send(self, text):
        """发一条文本消息。失败返回 False，由调用方计数。绝不抛异常、绝不无限等。"""
        if self._sock is None:
            if time.time() < self._next_attempt:
                return False
            try:
                self.connect()
            except WsClientError as exc:
                self.log("[vision] 对话节点未连上：%s" % exc)
                return False
        try:
            self._sock.sendall(ws_protocol.encode_frame(str(text), ws_protocol.OP_TEXT, mask=True))
            return True
        except OSError as exc:
            # 含 socket.timeout：可能已经写出半个帧，连接无法复用，直接断开重连
            self.last_error = "发送失败：%s" % exc
            self.close()
            self._note_failure()
            return False

    def close(self):
        if self._sock is not None:
            sock = self._sock
            self._sock = None       # 先摘掉引用：close 期间 connected 必须是 False
            try:
                sock.settimeout(0.2)    # 收尾帧不值得再等一个 send_timeout
                sock.sendall(ws_protocol.encode_frame(b"", ws_protocol.OP_CLOSE, mask=True))
            except OSError:
                pass
            try:
                sock.close()
            except OSError:
                pass

    def describe(self):
        state = "已连接" if self.connected else "未连接"
        detail = "（失败 %d 次）" % self.connect_failures
        if self.last_error:
            detail = "（失败 %d 次，最近错误：%s）" % (self.connect_failures, self.last_error)
        return "%s %s%s" % (self.url, state, detail)
