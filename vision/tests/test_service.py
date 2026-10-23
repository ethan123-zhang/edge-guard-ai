"""主循环联调（不接摄像头/模型）：检测→识别→稳定→事件→Sink 的整条线。

用假源 + 假检测器 + 真人脸库，把 `VisionService` 的接线钉死。
真实摄像头与模型是"外部世界"，这里只验证我们自己的代码有没有接错线。
"""
import os
import tempfile
import unittest

import numpy as np

from vision import config, events, gallery
from vision.detector import FaceDetection
from vision.recognizer import NullRecognizer
from vision.service import (JsonlSink, MultiSink, NullSink, VisionService,
                            build_sink)
from vision.tracker import FaceTracker


class FakeSource(object):
    def __init__(self, frame_size=(240, 320)):
        self.released = False
        self._size = frame_size

    def open(self):
        return self

    def read(self):
        height, width = self._size
        return True, np.zeros((height, width, 3), dtype=np.uint8), events.now_ms()

    def release(self):
        self.released = True

    def describe(self):
        return "fake-camera"


class FakeDetector(object):
    def __init__(self):
        self.calls = 0

    def detect(self, frame):
        self.calls += 1
        return [FaceDetection(100, 80, 80, 80, 0.97)]

    def describe(self):
        return "fake-detector"


class FakeRecognizer(object):
    available = True

    def embed(self, frame, detection):
        return np.array([1.0, 0.0, 0.0], dtype=np.float32)

    def describe(self):
        return "fake-recognizer"


class RecordingSink(object):
    name = "recording"

    def __init__(self):
        self.messages = []
        self.closed = False

    def send(self, msg):
        self.messages.append(msg)
        return True

    def close(self):
        self.closed = True

    def of_type(self, kind):
        return [m for m in self.messages if m.get("type") == kind]


def build_service(tmpdir, recognizer=None, confirm_frames=1):
    cfg = config.load("sim")
    cfg["recognition"]["confirm_frames"] = confirm_frames
    cfg["server"]["enabled"] = True
    cfg["server"]["heartbeat_sec"] = 0.02
    cfg["output"]["save_snapshots"] = False
    cfg["runtime"]["max_fps"] = 0

    store = gallery.FaceGallery(tmpdir).load()
    store.add("p_10023", "王阿姨", np.array([1.0, 0.0, 0.0]))

    sink = RecordingSink()
    service = VisionService(
        cfg, FakeSource(), FakeDetector(), recognizer or FakeRecognizer(),
        store, FaceTracker(confirm_frames=confirm_frames, cooldown_sec=10.0),
        sink, log=lambda msg: None,
    )
    return service, sink


class TestVisionService(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()

    def tearDown(self):
        self._tmp.cleanup()

    def test_known_face_produces_one_valid_face_event(self):
        service, sink = build_service(self._tmp.name)
        service.run(duration_sec=1.0,
                    should_stop=lambda: bool(sink.of_type("face_event")))
        face_events = sink.of_type("face_event")
        self.assertEqual(len(face_events), 1, "同一次停留只能上报一次")
        msg = face_events[0]
        self.assertEqual(events.check(msg), [])
        self.assertTrue(msg["is_known"])
        self.assertEqual(msg["name"], "王阿姨")
        self.assertEqual(msg["face_id"], "p_10023")
        self.assertGreater(msg["confidence"], 0.9)
        self.assertEqual(msg["bbox"], [100, 80, 80, 80])

    def test_heartbeat_is_sent(self):
        service, sink = build_service(self._tmp.name)
        service.run(duration_sec=1.0,
                    should_stop=lambda: bool(sink.of_type("vision_heartbeat")))
        beats = sink.of_type("vision_heartbeat")
        self.assertGreaterEqual(len(beats), 1)
        self.assertEqual(events.check(beats[0]), [])

    def test_without_sface_everyone_is_a_stranger(self):
        service, sink = build_service(self._tmp.name, recognizer=NullRecognizer())
        service.run(duration_sec=1.0,
                    should_stop=lambda: bool(sink.of_type("face_event")))
        msg = sink.of_type("face_event")[0]
        self.assertFalse(msg["is_known"])
        self.assertIsNone(msg["name"])
        self.assertTrue(msg["face_id"].startswith("unk_"))
        self.assertEqual(msg["confidence"], 0.0)
        self.assertEqual(events.check(msg), [])

    def test_confirm_frames_delays_the_event(self):
        service, sink = build_service(self._tmp.name, confirm_frames=3)
        service.run(duration_sec=0.25,
                    should_stop=lambda: service.stats.frames >= 2)
        self.assertEqual(sink.of_type("face_event"), [], "2 帧不该确认一个人")

    def test_sink_and_source_are_closed(self):
        service, sink = build_service(self._tmp.name)
        service.run(duration_sec=0.2, should_stop=lambda: service.stats.frames >= 1)
        self.assertTrue(sink.closed)
        self.assertTrue(service.source.released)


class TestBuildSink(unittest.TestCase):
    def test_jsonl_is_a_tee_not_a_replacement(self):
        cfg = config.load("sim")
        cfg["server"]["enabled"] = True
        with tempfile.TemporaryDirectory() as tmp:
            sink = build_sink(cfg, jsonl_path=os.path.join(tmp, "e.jsonl"))
            self.assertIsInstance(sink, MultiSink)
            self.assertIn("ws", sink.name)
            self.assertIn("jsonl", sink.name)
            sink.close()

    def test_without_server_only_jsonl(self):
        cfg = config.load("sim")
        cfg["server"]["enabled"] = False
        with tempfile.TemporaryDirectory() as tmp:
            sink = build_sink(cfg, jsonl_path=os.path.join(tmp, "e.jsonl"))
            self.assertEqual(sink.name, "jsonl")
            sink.close()

    def test_no_server_no_jsonl_is_null(self):
        cfg = config.load("sim")
        cfg["server"]["enabled"] = False
        self.assertIsInstance(build_sink(cfg), NullSink)


class TestMultiSink(unittest.TestCase):
    def test_failure_in_any_sink_fails_the_send(self):
        class Ok(object):
            name = "ok"

            def send(self, msg):
                return True

            def close(self):
                pass

        class Bad(Ok):
            name = "bad"

            def send(self, msg):
                return False

        multi = MultiSink([Ok(), Bad()])
        self.assertFalse(multi.send({"type": "x"}))


class TestRoi(unittest.TestCase):
    def test_crop_rect_full_frame_when_disabled(self):
        service, _sink = build_service(tempfile.mkdtemp())
        self.assertEqual(service._crop_rect((480, 640, 3)), (0, 0, 640, 480))

    def test_crop_rect_uses_normalized_roi(self):
        service, _sink = build_service(tempfile.mkdtemp())
        service._roi = (0.5, 0.5, 0.5, 0.5)
        self.assertEqual(service._crop_rect((480, 640, 3)), (320, 240, 320, 240))


if __name__ == "__main__":
    unittest.main()
