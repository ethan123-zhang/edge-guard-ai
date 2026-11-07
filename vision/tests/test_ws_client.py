"""WebSocket 客户端的有界超时（2026-11-07）。

现场教训：对端 `accept` 了但不应答（最阴的一种"连上了"）时，原来的握手读会一直
挂着；对端不读时 `sendall` 也会挂死（TCP 窗口满）。两种都会把主循环从 16.5 fps
拖到 8.2 fps，而日志上看只是"没再发出去"，现场很难定位。

这里用本地假对端把三条路径钉死：不说话 → 有界失败；正常 → 能发；不读 → 有界失败。
"""
import socket
import threading
import time
import unittest

from vision import ws_protocol
from vision.ws_client import WebSocketClient, WsClientError


class FakeServer(object):
    """本地假对端。mode: silent=accept 后装死 / stalled=握手后不读 / echo=正常。"""

    def __init__(self, mode="silent"):
        self.mode = mode
        self.received = []
        self._stop = False
        self._sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._sock.bind(("127.0.0.1", 0))
        self._sock.listen(1)
        self.port = self._sock.getsockname()[1]
        self._thread = threading.Thread(target=self._serve)
        self._thread.daemon = True
        self._thread.start()

    def _serve(self):
        try:
            conn, _addr = self._sock.accept()
        except OSError:
            return
        try:
            if self.mode == "silent":
                self._wait()
                return
            # 接收缓冲收小，让"对端不读"能快速把发送端卡住（否则回环缓冲太大）
            conn.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 2048)
            ws_protocol.server_handshake(conn)
            if self.mode == "stalled":
                self._wait()
                return
            while not self._stop:
                text = ws_protocol.recv_text(conn)
                if text is None:
                    break
                self.received.append(text)
        except (OSError, ws_protocol.WsError):
            pass
        finally:
            try:
                conn.close()
            except OSError:
                pass

    def _wait(self):
        while not self._stop:
            time.sleep(0.02)

    def close(self):
        self._stop = True
        try:
            self._sock.close()
        except OSError:
            pass
        self._thread.join(timeout=2.0)


class TestHandshakeTimeout(unittest.TestCase):
    def test_silent_peer_fails_within_the_configured_timeout(self):
        """对端 accept 了却不回 HTTP 头：必须在 timeout 内失败，不能永久挂着。"""
        server = FakeServer("silent")
        self.addCleanup(server.close)
        client = WebSocketClient("ws://127.0.0.1:%d" % server.port, timeout=0.3)
        started = time.time()
        with self.assertRaises(WsClientError):
            client.connect()
        elapsed = time.time() - started
        self.assertLess(elapsed, 2.0, "握手必须被 timeout 掐断（实测 %.2f s）" % elapsed)
        self.assertEqual(client.connect_failures, 1)
        self.assertIn("握手", client.last_error)

    def test_refused_port_fails_fast(self):
        probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
        probe.close()
        client = WebSocketClient("ws://127.0.0.1:%d" % port, timeout=0.3, reconnect_sec=0.0)
        started = time.time()
        self.assertFalse(client.send('{"type": "x"}'))
        self.assertLess(time.time() - started, 2.0)
        self.assertEqual(client.connect_failures, 1)


class TestSendTimeout(unittest.TestCase):
    def test_stalled_peer_does_not_block_send_forever(self):
        """对端握手后不读：sendall 会在 send_timeout 内失败并断开。

        回环链路上内核会先吞掉约 2 MB 才真正阻塞（Windows 实测 2.26 MB），
        所以这里连续发 64 KB 帧直到它返回 False —— 证明"写不进去时会失败"，
        而不是"整个主循环跟着挂住"。
        """
        server = FakeServer("stalled")
        self.addCleanup(server.close)
        client = WebSocketClient("ws://127.0.0.1:%d" % server.port,
                                 timeout=2.0, send_timeout=0.2)
        client.connect()
        self.assertTrue(client.connected)
        client._sock.setsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF, 4096)
        started = time.time()
        ok = True
        attempts = 0
        while ok and attempts < 80 and (time.time() - started) < 6.0:
            ok = client.send("x" * 65536)
            attempts += 1
        elapsed = time.time() - started
        self.assertFalse(ok, "对端不读时 send 必须返回 False（发了 %d 帧仍成功）" % attempts)
        self.assertLess(elapsed, 5.0, "send 必须被 send_timeout 掐断（实测 %.2f s）" % elapsed)
        self.assertFalse(client.connected, "超时后连接必须断开，下次重连")
        self.assertIn("发送失败", client.last_error)

    def test_timeout_while_sending_leaves_the_sink_recovering(self):
        """直接喂一个超时的 socket：失败计数、连接摘掉、不抛异常。"""

        class TimeoutSocket(object):
            def __init__(self):
                self.closed = False

            def settimeout(self, value):
                pass

            def sendall(self, data):
                raise socket.timeout("timed out")

            def close(self):
                self.closed = True

        client = WebSocketClient("ws://127.0.0.1:1", timeout=0.2, send_timeout=0.2)
        fake = TimeoutSocket()
        client._sock = fake
        started = time.time()
        self.assertFalse(client.send("{}"))
        self.assertLess(time.time() - started, 0.5, "假 socket 必须立刻返回")
        self.assertFalse(client.connected)
        self.assertTrue(fake.closed)
        self.assertIn("发送失败", client.last_error)

    def test_happy_path_still_sends_text(self):
        server = FakeServer("echo")
        self.addCleanup(server.close)
        client = WebSocketClient("ws://127.0.0.1:%d" % server.port, timeout=2.0)
        client.connect()
        self.assertTrue(client.send('{"type": "vision_heartbeat"}'))
        deadline = time.time() + 2.0
        while not server.received and time.time() < deadline:
            time.sleep(0.02)
        self.assertEqual(server.received, ['{"type": "vision_heartbeat"}'])
        client.close()
        self.assertFalse(client.connected)

    def test_reconnect_is_throttled_after_failure(self):
        server = FakeServer("silent")
        self.addCleanup(server.close)
        client = WebSocketClient("ws://127.0.0.1:%d" % server.port,
                                 timeout=0.2, reconnect_sec=60.0)
        self.assertFalse(client.send("{}"))
        started = time.time()
        self.assertFalse(client.send("{}"))
        self.assertLess(time.time() - started, 0.2, "reconnect_sec 内不许再试连接")
        self.assertEqual(client.connect_failures, 1)


class TestBackoff(unittest.TestCase):
    """连不上时按 1s → 2s → 4s … 退避，封顶 max_reconnect_sec。

    为什么要有退避：对端"半死"（accept 了不应答）时每次探测仍要花 `timeout`；
    固定 3 秒一探就是每 3 秒丢 1 秒，帧率会被拖塌。
    """

    def test_backoff_doubles_and_caps_then_resets_on_success(self):
        client = WebSocketClient("ws://127.0.0.1:1", reconnect_sec=1.0,
                                 max_reconnect_sec=8.0)
        delays = []
        for _ in range(6):
            client._note_failure()
            delays.append(round(client._next_attempt - client._last_attempt, 3))
        self.assertEqual(delays, [1.0, 2.0, 4.0, 8.0, 8.0, 8.0])
        client._note_success()
        self.assertEqual(client._fail_streak, 0)
        self.assertEqual(client._next_attempt, 0.0)


class TestDescribe(unittest.TestCase):
    def test_describe_surfaces_the_last_error(self):
        probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
        probe.close()
        client = WebSocketClient("ws://127.0.0.1:%d" % port, timeout=0.2)
        client.send("{}")
        text = client.describe()
        self.assertIn("未连接", text)
        self.assertIn("最近错误", text)


if __name__ == "__main__":
    unittest.main()