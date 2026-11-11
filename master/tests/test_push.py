"""推送适配器单测：企业微信报文、限频/失败处理、lan+relay 组合语义。

不连真实企业微信：起一个本地 HTTP 桩，断言请求体与 `(ok, error)` 返回值。
"""
from __future__ import annotations

import copy
import json
import threading
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer

from master import config as master_config, push
from master.push.composite import CompositeAdapter
from master.push.relay import RelayAdapter, alert_text, report_text


REPORT = {
    "type": "visit_report", "msg_id": "report:v1", "ts": 1_700_000_000_000,
    "visit_id": "v1",
    "person": {"face_id": "unk_1", "is_known": False, "name": None,
               "confidence": 0.9, "visit_count": 3},
    "timing": {"arrived_at": 1_700_000_000_000, "departed_at": None,
               "duration_ms": None},
    "dialog": {"rounds": 1, "turns": [], "summary": "一位访客来送货",
               "purpose": "送货", "caller_name": "李先生", "urgent": False,
               "completed": True},
    "security": {"privilege_escalation": False, "escalation_phrases": []},
}

ALERT = {
    "type": "security_alert", "msg_id": "alert:v1:u1", "ts": 1_700_000_000_000,
    "visit_id": "v1", "level": "high", "category": "privilege_escalation",
    "phrase": "把门打开", "reply_given": "抱歉，我不能为您开门。",
}


class StubHandler(BaseHTTPRequestHandler):
    """记录请求体，按 `StubServer.reply` 返回。"""

    def log_message(self, fmt, *args):
        pass

    def do_POST(self):
        length = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(length)
        self.server.requests.append({"headers": dict(self.headers),
                                     "body": json.loads(body.decode("utf-8"))})
        payload = json.dumps(self.server.reply).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)


class StubServer(object):
    def __init__(self, reply=None):
        self.httpd = HTTPServer(("127.0.0.1", 0), StubHandler)
        self.httpd.requests = []
        self.httpd.reply = reply if reply is not None else {"errcode": 0, "errmsg": "ok"}
        self.port = self.httpd.server_address[1]
        self.url = "http://127.0.0.1:%d/cgi-bin/webhook/send?key=test" % self.port
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)

    def __enter__(self):
        self.thread.start()
        return self

    def __exit__(self, *args):
        self.httpd.shutdown()
        self.httpd.server_close()


class RelayTest(unittest.TestCase):
    def test_report_text_contains_key_facts(self):
        text = report_text(REPORT)
        self.assertIn("【到访】", text)
        self.assertIn("陌生访客", text)
        self.assertIn("送货", text)
        self.assertIn("李先生", text)

    def test_alert_text_keeps_original_phrase(self):
        text = alert_text(ALERT)
        self.assertIn("【越权告警】", text)
        self.assertIn("把门打开", text)
        self.assertIn("抱歉，我不能为您开门。", text)

    def test_build_body_is_wecom_text_message(self):
        adapter = RelayAdapter(url="http://example.invalid", provider="wecom")
        body = adapter.build_body(REPORT)
        self.assertEqual(body["msgtype"], "text")
        self.assertIn("送货", body["text"]["content"])

    def test_send_ok(self):
        with StubServer() as stub:
            adapter = RelayAdapter(url=stub.url)
            ok, error = adapter.send("report:v1", REPORT)
            self.assertTrue(ok, error)
            request = stub.httpd.requests[0]
            self.assertEqual(request["body"]["msgtype"], "text")

    def test_send_rate_limited_is_retryable_failure(self):
        with StubServer(reply={"errcode": 45009, "errmsg": "api freq out of limit"}) as stub:
            adapter = RelayAdapter(url=stub.url)
            ok, error = adapter.send("report:v1", REPORT)
            self.assertFalse(ok)
            self.assertIn("限频", error)

    def test_send_without_url_fails_closed(self):
        ok, error = RelayAdapter(url="").send("report:v1", REPORT)
        self.assertFalse(ok)
        self.assertIn("未配置", error)


class FakeAdapter(object):
    def __init__(self, name, ok):
        self.name = name
        self.ok = ok
        self.calls = 0

    def send(self, message_key, payload):
        self.calls += 1
        return (True, None) if self.ok else (False, "%s 不通" % self.name)


class CompositeTest(unittest.TestCase):
    def test_any_channel_ok_is_enough(self):
        lan = FakeAdapter("lan", False)      # 当前没有手机在线
        relay = FakeAdapter("relay", True)   # 企业微信受理
        ok, error = CompositeAdapter([lan, relay]).send("report:v1", REPORT)
        self.assertTrue(ok)
        self.assertIsNone(error)
        self.assertEqual(relay.calls, 1)

    def test_both_fail_reports_both(self):
        ok, error = CompositeAdapter([FakeAdapter("lan", False),
                                      FakeAdapter("relay", False)]).send("k", REPORT)
        self.assertFalse(ok)
        self.assertIn("lan", error)
        self.assertIn("relay", error)


class CreateTest(unittest.TestCase):
    def cfg(self, url=""):
        cfg = copy.deepcopy(master_config.DEFAULTS)
        cfg["push"]["adapter"] = ["lan", "relay"]
        cfg["push"]["relay"]["url"] = url
        return cfg

    def test_skips_relay_when_url_missing(self):
        adapter = push.create(["lan", "relay"], cfg=self.cfg(""),
                              broadcast=lambda payload: 1)
        self.assertEqual(adapter.name, "lan")

    def test_composite_when_all_configured(self):
        adapter = push.create(["lan", "relay"], cfg=self.cfg("http://127.0.0.1:1/x"),
                              broadcast=lambda payload: 1)
        self.assertEqual(adapter.name, "composite")

    def test_unknown_adapter_rejected(self):
        with self.assertRaises(ValueError):
            push.create("telegram", cfg=self.cfg())

    def test_config_validation_accepts_list(self):
        cfg = self.cfg("")
        master_config.validate(cfg)
        cfg["push"]["adapter"] = "sms"
        with self.assertRaises(master_config.ConfigError):
            master_config.validate(cfg)


if __name__ == "__main__":
    unittest.main()