"""WebSocket 协议测试：帧编码/解码、握手、跨端互通。

用手工实现的协议就等于"自己写的东西必须自己验"：
单测里起一对 socketpair，一边当服务端（不 mask），一边当客户端（必须 mask）。
"""
import socket
import threading
import unittest

from vision import ws_protocol


class TestFrames(unittest.TestCase):
    def _roundtrip(self, payload):
        left, right = socket.socketpair()
        try:
            left.sendall(ws_protocol.encode_frame(payload, ws_protocol.OP_TEXT, mask=True))
            opcode, received = ws_protocol.recv_frame(right)
        finally:
            left.close()
            right.close()
        return opcode, received

    def test_short_text_frame_roundtrip(self):
        opcode, payload = self._roundtrip("你好，face_event")
        self.assertEqual(opcode, ws_protocol.OP_TEXT)
        self.assertEqual(payload.decode("utf-8"), "你好，face_event")

    def test_medium_frame_uses_16bit_length(self):
        text = "x" * 300
        opcode, payload = self._roundtrip(text)
        self.assertEqual(len(payload), 300)

    def test_large_frame_uses_64bit_length(self):
        text = "y" * 70000
        _opcode, payload = self._roundtrip(text)
        self.assertEqual(len(payload), 70000)

    def test_accept_key_matches_rfc_example(self):
        # RFC6455 §1.3 的样例
        self.assertEqual(
            ws_protocol.accept_key("dGhlIHNhbXBsZSBub25jZQ=="),
            "s3pPLMBiTxaQ9kYGzzhZRbK+xOo=",
        )


class TestHandshake(unittest.TestCase):
    def test_client_and_server_agree(self):
        left, right = socket.socketpair()
        captured = {}

        def serve():
            try:
                ws_protocol.server_handshake(right)
                captured["text"] = ws_protocol.recv_text(right)
                right.sendall(ws_protocol.encode_frame("pong", ws_protocol.OP_TEXT))
            finally:
                right.close()

        thread = threading.Thread(target=serve)
        thread.daemon = True
        thread.start()
        try:
            request, key = ws_protocol.client_handshake_request("127.0.0.1", 8765)
            left.sendall(request)
            start_line, headers = ws_protocol.read_http_head(left)
            ws_protocol.verify_server_response(start_line, headers, key)
            left.sendall(ws_protocol.encode_frame("hello", mask=True))
            opcode, payload = ws_protocol.recv_frame(left)
        finally:
            left.close()
        thread.join(3)
        self.assertEqual(captured.get("text"), "hello")
        self.assertEqual(opcode, ws_protocol.OP_TEXT)
        self.assertEqual(payload, b"pong")

    def test_rejects_tampered_accept_header(self):
        with self.assertRaises(ws_protocol.WsError):
            ws_protocol.verify_server_response(
                "HTTP/1.1 101 Switching Protocols",
                {"sec-websocket-accept": "wrong"},
                "dGhlIHNhbXBsZSBub25jZQ==",
            )


if __name__ == "__main__":
    unittest.main()
