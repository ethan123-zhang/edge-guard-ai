"""图片推送（vision → 主人端 `/vision/snapshot`）的接线。

为什么要后台线程 + 有界队列：主循环 17 fps，一条 POST 要建 TCP、写 100KB、读响应；
对端没起/卡住时同步发会把帧率拖到 8 fps 以下（`ws_client` 2026-11-07 已踩过）。
所以入队必须**永不阻塞**，队列满就丢并计数，失败只记账不抛；`close()` 先把队列发完再退。

本机 `http.server` 收包，验证"发出去的到底是什么"（路径/查询串/共享密钥/JPEG 原字节）。
服务侧用 `vision.tests.test_service` 的假源+假检测器，只验证接线，不碰摄像头与模型。
"""
import os
import tempfile
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import parse_qs, urlparse

import numpy as np

from vision import config, gallery
from vision.service import VisionService
from vision.snapshot_push import SnapshotPusher, build_pusher
from vision.tests.test_service import (FakeDetector, FakeRecognizer, FakeSource,
                                       RecordingSink, textured_frame)
from vision.tracker import FaceTracker

KEY = "vision-test-key"


# ---------------------------------------------------------------- 假主人端

class CaptureHandler(BaseHTTPRequestHandler):
    """记录每个 POST 的路径/请求头/原字节，按 `status` 回状态码。"""

    requests = []
    status = 200

    def do_POST(self):
        length = int(self.headers.get("content-length") or 0)
        body = self.rfile.read(length) if length else b""
        type(self).requests.append({
            "path": self.path,
            "headers": dict(self.headers.items()),
            "body": body,
        })
        self.send_response(type(self).status)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(b'{"ok": true}')

    def log_message(self, *args):   # 测试里不要把访问日志刷到终端
        pass


class FakeMaster(object):
    """本机 127.0.0.1 上的收包服务；端口由系统分配，测试间互不干扰。"""

    def __init__(self):
        CaptureHandler.requests = []
        CaptureHandler.status = 200
        self.httpd = HTTPServer(("127.0.0.1", 0), CaptureHandler)
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()
        self.url = "http://127.0.0.1:%d/vision/snapshot" % self.httpd.server_port

    def captured(self):
        return CaptureHandler.requests

    def set_status(self, status):
        CaptureHandler.status = int(status)

    def wait_for(self, count, timeout=5.0):
        deadline = time.time() + timeout
        while time.time() < deadline:
            if len(CaptureHandler.requests) >= count:
                return True
            time.sleep(0.01)
        return False

    def close(self):
        self.httpd.shutdown()
        self.httpd.server_close()


def wait_until(predicate, timeout=5.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if predicate():
            return True
        time.sleep(0.01)
    return False


# ---------------------------------------------------------------- 推送器

class TestSnapshotPusher(unittest.TestCase):
    def setUp(self):
        self.master = FakeMaster()
        self.addCleanup(self.master.close)
        self.pusher = SnapshotPusher(self.master.url, KEY, timeout=2.0, queue_size=4,
                                     log=lambda msg: None)
        self.addCleanup(self.pusher.close)

    def test_posts_jpeg_to_vision_snapshot_with_shared_key(self):
        data = b"\xff\xd8\xff" + b"x" * 64
        self.assertTrue(self.pusher.enqueue("msg-1", "unk_0", 1760000000000, data))
        self.assertTrue(self.master.wait_for(1), "5 秒内没收到推图请求")

        request = self.master.captured()[0]
        parsed = urlparse(request["path"])
        self.assertEqual(parsed.path, "/vision/snapshot")
        query = parse_qs(parsed.query)
        self.assertEqual(query["msg_id"], ["msg-1"])
        self.assertEqual(query["face_id"], ["unk_0"])
        self.assertEqual(query["frame_ts"], ["1760000000000"])
        self.assertEqual(request["headers"].get("X-Vision-Key"), KEY)
        self.assertEqual(request["headers"].get("Content-Type"), "image/jpeg")
        self.assertEqual(request["body"], data)

        self.assertTrue(wait_until(lambda: self.pusher.sent >= 1))
        self.assertEqual(self.pusher.sent, 1)
        self.assertEqual(self.pusher.failed, 0)

    def test_failed_upload_counts_error_and_worker_survives(self):
        self.master.set_status(500)
        self.pusher.enqueue("msg-bad", "unk_1", 1760000000001, b"\xff\xd8bad")
        self.assertTrue(wait_until(lambda: self.pusher.failed >= 1))
        self.assertEqual(self.pusher.sent, 0)
        self.assertIn("500", self.pusher.last_error)

        self.master.set_status(200)
        self.pusher.enqueue("msg-good", "unk_1", 1760000000002, b"\xff\xd8good")
        self.assertTrue(wait_until(lambda: self.pusher.sent >= 1),
                        "一次失败之后工作线程必须还活着")
        self.assertEqual(len(self.master.captured()), 2)

    def test_unreachable_master_never_raises_and_counts_failure(self):
        pusher = SnapshotPusher("http://127.0.0.1:9/vision/snapshot", KEY,
                                timeout=0.2, queue_size=2, log=lambda msg: None)
        self.addCleanup(pusher.close)
        self.assertTrue(pusher.enqueue("m", "f", 1, b"\xff\xd8z"))
        self.assertTrue(wait_until(lambda: pusher.failed >= 1, timeout=5.0))
        self.assertEqual(pusher.sent, 0)
        self.assertTrue(pusher.last_error)

    def test_full_queue_drops_newest_instead_of_blocking(self):
        class SlowPusher(SnapshotPusher):
            def _post(self, item):
                time.sleep(0.2)

        pusher = SlowPusher(self.master.url, KEY, timeout=2.0, queue_size=1,
                            log=lambda msg: None)
        self.addCleanup(pusher.close)
        started = time.time()
        results = [pusher.enqueue("m%d" % i, "unk_0", i, b"\xff\xd8x") for i in range(6)]
        elapsed = time.time() - started

        self.assertLess(elapsed, 0.1, "队列满时入队不能阻塞主循环")
        self.assertIn(False, results, "队列满时应有条目被丢弃")
        self.assertGreaterEqual(pusher.dropped, 1)

    def test_close_flushes_pending_then_stops_worker(self):
        class SlowPusher(SnapshotPusher):
            def _post(self, item):
                super(SlowPusher, self)._post(item)
                time.sleep(0.05)

        pusher = SlowPusher(self.master.url, KEY, timeout=2.0, queue_size=8,
                            log=lambda msg: None)
        for i in range(3):
            pusher.enqueue("m%d" % i, "unk_0", i, b"\xff\xd8y")
        pusher.close(timeout=5.0)

        self.assertEqual(len(self.master.captured()), 3, "close() 必须把队列里的发完")
        self.assertFalse(pusher.thread_alive())

    def test_bad_url_is_rejected_at_construction(self):
        with self.assertRaises(ValueError):
            SnapshotPusher("ws://127.0.0.1:8766/vision/snapshot", KEY)
        with self.assertRaises(ValueError):
            SnapshotPusher("http://127.0.0.1:8766", "")


class TestBuildPusher(unittest.TestCase):
    def setUp(self):
        self.master = FakeMaster()
        self.addCleanup(self.master.close)

    def _cfg(self, **push):
        cfg = config.load("sim")
        settings = {"enabled": False, "url": "", "key": "", "timeout": 2.0, "queue_size": 8}
        settings.update(push)
        cfg["output"]["snapshot_push"] = settings
        return cfg

    def test_disabled_returns_none(self):
        self.assertIsNone(build_pusher(self._cfg(), log=lambda msg: None))

    def test_key_falls_back_to_environment_variable(self):
        old = os.environ.get("SENTINEL_VISION_KEY")
        os.environ["SENTINEL_VISION_KEY"] = "env-key"
        try:
            pusher = build_pusher(self._cfg(enabled=True, url=self.master.url),
                                  log=lambda msg: None)
        finally:
            if old is None:
                os.environ.pop("SENTINEL_VISION_KEY", None)
            else:
                os.environ["SENTINEL_VISION_KEY"] = old
        self.assertIsNotNone(pusher)
        self.addCleanup(pusher.close)
        pusher.enqueue("m", "f", 1, b"\xff\xd8k")
        self.assertTrue(self.master.wait_for(1))
        self.assertEqual(self.master.captured()[0]["headers"].get("X-Vision-Key"),
                         "env-key")

    def test_missing_key_disables_push_with_a_loud_log(self):
        old = os.environ.pop("SENTINEL_VISION_KEY", None)
        logs = []
        try:
            pusher = build_pusher(self._cfg(enabled=True, url=self.master.url),
                                  log=logs.append)
        finally:
            if old is not None:
                os.environ["SENTINEL_VISION_KEY"] = old
        self.assertIsNone(pusher)
        joined = "\n".join(logs)
        self.assertIn("SENTINEL_VISION_KEY", joined)


# ---------------------------------------------------------------- 配置

class TestSnapshotPushConfig(unittest.TestCase):
    def test_sim_profile_keeps_push_off(self):
        push = config.load("sim")["output"]["snapshot_push"]
        self.assertFalse(push["enabled"])
        self.assertEqual(push["key"], "", "密钥不能写进仓库里的 yaml")

    def test_board_profile_points_at_lubancat_without_a_key(self):
        push = config.load("board")["output"]["snapshot_push"]
        self.assertTrue(push["enabled"], "板上必须打开推图，否则看板永远缺图")
        url = push["url"]
        self.assertTrue(url.startswith("http://"), url)
        self.assertIn("192.168.5.11", url)
        self.assertIn("/vision/snapshot", url)
        self.assertEqual(push["key"], "")

    def test_enabled_requires_an_http_url(self):
        cfg = config.load("sim")
        cfg["output"]["snapshot_push"] = {"enabled": True, "url": "", "key": "",
                                          "timeout": 2.0, "queue_size": 8}
        with self.assertRaises(config.ConfigError) as ctx:
            config.validate(cfg)
        self.assertIn("output.snapshot_push.url", str(ctx.exception))

    def test_non_positive_timeout_or_queue_is_rejected(self):
        cfg = config.load("sim")
        cfg["output"]["snapshot_push"] = {"enabled": True,
                                          "url": "http://127.0.0.1:8766/vision/snapshot",
                                          "key": "", "timeout": 0, "queue_size": 8}
        with self.assertRaises(config.ConfigError):
            config.validate(cfg)
        cfg["output"]["snapshot_push"]["timeout"] = 2.0
        cfg["output"]["snapshot_push"]["queue_size"] = 0
        with self.assertRaises(config.ConfigError):
            config.validate(cfg)


# ---------------------------------------------------------------- 服务接线

def build_push_service(tmpdir, push, save_snapshots=False, snapshot_dir=None):
    """和 test_service.build_service 同样的假源/假检测器，只是把推图配置挂上去。"""
    cfg = config.load("sim")
    cfg["recognition"]["confirm_frames"] = 1
    cfg["server"]["enabled"] = False
    cfg["output"]["save_snapshots"] = bool(save_snapshots)
    cfg["output"]["snapshot_push"] = push
    if snapshot_dir is not None:
        cfg["output"]["snapshot_dir"] = snapshot_dir
    cfg["runtime"]["max_fps"] = 0

    store = gallery.FaceGallery(tmpdir).load()
    store.add("p_10023", "王阿姨", np.array([1.0, 0.0, 0.0], dtype=np.float32))

    sink = RecordingSink()
    logs = []
    service = VisionService(
        cfg, FakeSource(), FakeDetector(), FakeRecognizer(),
        store, FaceTracker(confirm_frames=1, cooldown_sec=10.0), sink,
        log=logs.append,
    )
    return service, sink, logs


class TestServicePushesSnapshot(unittest.TestCase):
    def setUp(self):
        self.master = FakeMaster()
        self.addCleanup(self.master.close)
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)

    def _push(self, **overrides):
        settings = {"enabled": True, "url": self.master.url, "key": KEY,
                    "timeout": 2.0, "queue_size": 4}
        settings.update(overrides)
        return settings

    def test_photo_uses_the_same_msg_id_as_the_event(self):
        service, sink, _logs = build_push_service(self._tmp.name, self._push())
        service.run(duration_sec=2.0,
                    should_stop=lambda: bool(sink.of_type("face_event")))
        events = sink.of_type("face_event")
        self.assertEqual(len(events), 1)
        self.assertTrue(self.master.wait_for(1), "事件到了但照片没推过来")

        request = self.master.captured()[0]
        query = parse_qs(urlparse(request["path"]).query)
        self.assertEqual(query["msg_id"], [events[0]["msg_id"]],
                         "照片必须按产生它的事件 msg_id 绑定")
        self.assertEqual(query["face_id"], [events[0]["face_id"]])
        self.assertEqual(query["frame_ts"], [str(events[0]["frame_ts"])])
        self.assertTrue(request["body"].startswith(b"\xff\xd8"),
                        "推的必须是 JPEG 字节")

    def test_photo_push_needs_no_local_snapshot_file(self):
        snapshot_dir = os.path.join(self._tmp.name, "snaps")
        service, sink, _logs = build_push_service(
            self._tmp.name, self._push(), save_snapshots=False,
            snapshot_dir=snapshot_dir)
        service.run(duration_sec=2.0,
                    should_stop=lambda: bool(sink.of_type("face_event")))
        self.assertTrue(self.master.wait_for(1))
        self.assertFalse(os.path.isdir(snapshot_dir), "关掉本地落盘时不该建目录")

    def test_local_snapshot_is_still_written_when_saving_is_on(self):
        snapshot_dir = os.path.join(self._tmp.name, "snaps")
        service, sink, _logs = build_push_service(
            self._tmp.name, self._push(enabled=False), save_snapshots=True,
            snapshot_dir=snapshot_dir)
        service.run(duration_sec=2.0,
                    should_stop=lambda: bool(sink.of_type("face_event")))
        files = []
        for root, _dirs, names in os.walk(snapshot_dir):
            files.extend(os.path.join(root, name) for name in names)
        self.assertEqual(len(files), 1, "保存开关打开时本地仍要落一张图")
        with open(files[0], "rb") as fh:
            self.assertTrue(fh.read(2) == b"\xff\xd8")

    def test_pusher_is_closed_when_the_loop_exits(self):
        service, sink, _logs = build_push_service(self._tmp.name, self._push())
        self.assertIsNotNone(service._pusher)
        service.run(duration_sec=2.0,
                    should_stop=lambda: bool(sink.of_type("face_event")))
        self.assertFalse(service._pusher.thread_alive(), "主循环退出必须收掉推图线程")

    def test_disabled_push_never_calls_the_master(self):
        service, sink, _logs = build_push_service(self._tmp.name,
                                                  self._push(enabled=False))
        self.assertIsNone(service._pusher)
        service.run(duration_sec=2.0,
                    should_stop=lambda: bool(sink.of_type("face_event")))
        time.sleep(0.2)
        self.assertEqual(self.master.captured(), [])


if __name__ == "__main__":
    unittest.main()