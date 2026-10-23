"""WebSocket 客户端：把事件发给对话节点（docs/02 §3，端口 8765）。

行为约定（现场排障要看这个）：
- `send()` **不抛异常**，返回 True/False。视觉节点是"生产者"，网络断了不能把
  主循环拖死 —— 断开时计失败数，由 `Service` 决定重连节奏（heartbeat 里体现）。
- 重连节流：两次连接尝试之间至少间隔 `reconnect_sec`，否则 1 秒能打出几十条拒绝。
"""
import socket
import time
from urllib.parse import urlparse

from vision import ws_protocol


class WsClientError(RuntimeError):
    pass


class WebSocketClient(object):
    def __init__(self, url, timeout=5.0, reconnect_sec=3.0, log=None):
        self.url = str(url)
        self.timeout = float(timeout)
        self.reconnect_sec = float(reconnect_sec)
        self.log = log or (lambda msg: None)
        self._sock = None
        self._host = None
        self._port = None
        self._path = "/"
        self._parse_url()
        self._last_attempt = 0.0
        self.connect_failures = 0

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

    def connect(self):
        """连不上抛 WsClientError；调用方可以忽略并稍后重试。"""
        self.close()
        try:
            sock = socket.create_connection((self._host, self._port), timeout=self.timeout)
        except OSError as exc:
            self.connect_failures += 1
            self._last_attempt = time.time()
            raise WsClientError("连不上 %s:%d（%s）" % (self._host, self._port, exc))
        sock.settimeout(None)   # 握手后恢复阻塞，避免 send 超时把帧截断
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
            self.connect_failures += 1
            self._last_attempt = time.time()
            raise WsClientError("WebSocket 握手失败（%s）" % exc)
        self._sock = sock
        self._last_attempt = time.time()
        return self

    def send(self, text):
        """发一条文本消息。失败返回 False，由调用方计数。"""
        if self._sock is None:
            if (time.time() - self._last_attempt) < self.reconnect_sec:
                return False
            try:
                self.connect()
            except WsClientError as exc:
                self.log("[vision] 对话节点未连上：%s" % exc)
                return False
        try:
            self._sock.sendall(ws_protocol.encode_frame(str(text), ws_protocol.OP_TEXT, mask=True))
            return True
        except OSError:
            self.close()
            return False

    def close(self):
        if self._sock is not None:
            try:
                self._sock.sendall(ws_protocol.encode_frame(b"", ws_protocol.OP_CLOSE, mask=True))
            except OSError:
                pass
            try:
                self._sock.close()
            except OSError:
                pass
            self._sock = None

    def describe(self):
        state = "已连接" if self.connected else "未连接"
        return "%s %s（失败 %d 次）" % (self.url, state, self.connect_failures)
