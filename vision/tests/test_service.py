"""主循环联调（不接摄像头/模型）：检测→识别→稳定→事件→Sink 的整条线。

用假源 + 假检测器 + 真人脸库，把 `VisionService` 的接线钉死。
真实摄像头与模型是"外部世界"，这里只验证我们自己的代码有没有接错线。
"""
import os
import tempfile
import unittest

import numpy as np

from vision import align, config, events, gallery
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
    backend = "fake-detector"

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


class BatchRecognizer(object):
    """带 `embed_batch` 的识别器：记录批量/逐张调用次数，可模拟"返回少了几个"。"""

    available = True

    def __init__(self, shorten=False):
        self.shorten = bool(shorten)
        self.batch_calls = []
        self.single_calls = 0

    def embed_batch(self, frame, detections):
        self.batch_calls.append(len(detections))
        if self.shorten:
            return [np.array([1.0, 0.0, 0.0], dtype=np.float32)] * (len(detections) - 1)
        return [np.array([1.0, 0.0, 0.0], dtype=np.float32) for _ in detections]

    def embed(self, frame, detection):
        self.single_calls += 1
        return np.array([1.0, 0.0, 0.0], dtype=np.float32)

    def describe(self):
        return "batch-fake"


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


def build_service(tmpdir, recognizer=None, confirm_frames=1, detector=None):
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
        cfg, FakeSource(), detector or FakeDetector(), recognizer or FakeRecognizer(),
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


class TestBatchRecognition(unittest.TestCase):
    """一帧一次批量识别（2026-11-07 P1）：有 embed_batch 就用，且不能把特征对错位。"""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)

    def _service(self, recognizer):
        service, sink = build_service(self._tmp.name, recognizer=recognizer)
        return service, sink

    def test_batch_recognizer_is_called_once_per_frame(self):
        rec = BatchRecognizer()
        service, sink = self._service(rec)
        service.run(duration_sec=1.0, should_stop=lambda: bool(sink.of_type("face_event")))
        self.assertEqual(rec.single_calls, 0, "有批量接口就不该逐张调用")
        self.assertTrue(rec.batch_calls)
        self.assertTrue(all(count == 1 for count in rec.batch_calls))
        self.assertTrue(sink.of_type("face_event")[0]["is_known"])

    def test_all_faces_of_a_frame_go_into_one_batch_call(self):
        rec = BatchRecognizer()
        service, _sink = self._service(rec)
        frame = np.zeros((240, 320, 3), dtype=np.uint8)
        dets = [FaceDetection(10, 10, 40, 40, 0.9), FaceDetection(120, 10, 40, 40, 0.9)]
        vectors = service._embeddings(frame, dets)
        self.assertEqual(len(vectors), 2)
        self.assertEqual(rec.batch_calls, [2], "两张脸必须合成一次批量前向")

    def test_short_batch_answer_falls_back_to_single_embed(self):
        """批量返回数量不对会把特征和脸对错位，必须退回逐张而不是硬凑。"""
        rec = BatchRecognizer(shorten=True)
        service, _sink = self._service(rec)
        frame = np.zeros((240, 320, 3), dtype=np.uint8)
        dets = [FaceDetection(10, 10, 40, 40, 0.9), FaceDetection(120, 10, 40, 40, 0.9)]
        vectors = service._embeddings(frame, dets)
        self.assertEqual(len(vectors), 2)
        self.assertEqual(rec.batch_calls, [2])
        self.assertEqual(rec.single_calls, 2)

    def test_no_faces_means_no_recognizer_call(self):
        rec = BatchRecognizer()
        service, _sink = self._service(rec)
        self.assertEqual(service._embeddings(np.zeros((240, 320, 3), dtype=np.uint8), []), [])
        self.assertEqual(rec.batch_calls, [])


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


class ScriptedDetector(object):
    """按调用方给的结果返回检测；用来在测试里制造"人在场/人走了/又回来"。"""

    backend = "scripted"

    def __init__(self):
        self.result = []

    def detect(self, frame):
        return list(self.result)

    def describe(self):
        return "scripted-detector"


class ConstantRecognizer(object):
    """每张脸都返回同一个向量：同一人；换向量就是"换了个人"。"""

    available = True

    def __init__(self, vector):
        self.vector = np.asarray(vector, dtype=np.float32)

    def embed(self, frame, detection):
        return self.vector.copy()

    def describe(self):
        return "constant-recognizer"


def template_landmarks(x=0.0, y=0.0, scale=2.0, distort=0.0):
    """按 SFace 模板缩放平移出来的 5 个关键点；distort 只拽嘴角（质量会变差）。"""
    points = align.ALIGN_TARGET * scale + np.array([x, y], dtype=np.float64)
    if distort:
        points = points.copy()
        points[4] += np.array([float(distort), 0.0])
    return points


def face_with_landmarks(bbox=(100, 80, 80, 80), score=0.97, distort=0.0):
    points = template_landmarks(bbox[0], bbox[1], 2.0, distort)
    raw = np.zeros(15, dtype=np.float32)
    raw[0:4] = bbox
    raw[4:14] = points.reshape(-1)
    raw[14] = score
    return FaceDetection(int(bbox[0]), int(bbox[1]), int(bbox[2]), int(bbox[3]), score,
                         landmarks=[(float(px), float(py)) for px, py in points],
                         raw_row=raw)


def build_assoc_service(tmpdir, recognizer, detector, assoc=None, confirm_frames=1):
    """可开关关联层的服务：其余与 build_service 一致，免得两套夹具漂移。"""
    cfg = config.load("sim")
    cfg["association"] = dict(config.DEFAULTS["association"])
    cfg["association"].update(assoc or {})
    cfg["recognition"]["confirm_frames"] = confirm_frames
    cfg["server"]["enabled"] = True
    cfg["server"]["heartbeat_sec"] = 0.02
    cfg["output"]["save_snapshots"] = False
    cfg["runtime"]["max_fps"] = 0

    store = gallery.FaceGallery(tmpdir).load()
    store.add("p_10023", "王阿姨", np.array([1.0, 0.0, 0.0]))

    sink = RecordingSink()
    service = VisionService(
        cfg, FakeSource(), detector, recognizer, store,
        FaceTracker(confirm_frames=confirm_frames, cooldown_sec=10.0),
        sink, log=lambda msg: None,
    )
    return service, sink


def step(service, detector, frame_ts, detections):
    """驱动一帧：`_process_frame` 是纯函数式的，不用真跑主循环。"""
    detector.result = detections
    service._process_frame(np.zeros((240, 320, 3), dtype=np.uint8), frame_ts)


class TestAssociationWiring(unittest.TestCase):
    """两层去重的第 2 层：同一个人出画再回来不能报第二条（P2.5）。"""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()

    def tearDown(self):
        self._tmp.cleanup()

    def _visit_twice(self, service, detector, first=None, second=None):
        first = first if first is not None else face_with_landmarks()
        second = second if second is not None else face_with_landmarks(bbox=(104, 82, 80, 80))
        for ts in (0, 500, 1000):
            step(service, detector, ts, [first])
        for ts in (1500, 2000, 2500, 3000, 3500):   # 离开超过轨道 TTL（2s）
            step(service, detector, ts, [])
        for ts in (4000, 4500):                     # 轨道重建，同一个人回来
            step(service, detector, ts, [second])

    def test_same_person_returning_is_not_reported_twice(self):
        detector = ScriptedDetector()
        service, sink = build_assoc_service(
            self._tmp.name, ConstantRecognizer([0.0, 1.0, 0.0]), detector,
            {"enabled": True})
        self._visit_twice(service, detector)
        reported = sink.of_type("face_event")
        self.assertEqual(len(reported), 1, "同一次连续在场只能有一条事件")
        self.assertEqual(service.stats.suppressed, 1)
        self.assertFalse(reported[0]["is_known"])

    def test_different_person_is_reported_again(self):
        detector = ScriptedDetector()
        recognizer = ConstantRecognizer([0.0, 1.0, 0.0])
        service, sink = build_assoc_service(
            self._tmp.name, recognizer, detector, {"enabled": True})
        first = face_with_landmarks()
        for ts in (0, 500, 1000):
            step(service, detector, ts, [first])
        for ts in (1500, 2000, 2500, 3000, 3500):   # 第一个人离开
            step(service, detector, ts, [])
        recognizer.vector = np.array([0.0, 0.0, 1.0], dtype=np.float32)  # 换了个人
        second = face_with_landmarks(bbox=(104, 82, 80, 80))
        for ts in (4000, 4500):
            step(service, detector, ts, [second])
        self.assertEqual(len(sink.of_type("face_event")), 2, "不是同一个人就要各报各的")
        self.assertEqual(service.stats.suppressed, 0)

    def test_bad_quality_evidence_never_merges(self):
        """质量不过门就不入册 —— 宁可重报，也不能把两个人合成一个会话。"""
        detector = ScriptedDetector()
        service, sink = build_assoc_service(
            self._tmp.name, ConstantRecognizer([0.0, 1.0, 0.0]), detector,
            {"enabled": True})
        bad = face_with_landmarks(distort=200.0)
        self._visit_twice(service, detector, first=bad, second=bad)
        self.assertEqual(len(sink.of_type("face_event")), 2)
        self.assertEqual(service.stats.suppressed, 0)

    def test_disabled_association_keeps_the_old_behaviour(self):
        detector = ScriptedDetector()
        service, sink = build_assoc_service(
            self._tmp.name, ConstantRecognizer([0.0, 1.0, 0.0]), detector, {})
        self.assertFalse(service.associator.enabled)
        self._visit_twice(service, detector)
        self.assertEqual(len(sink.of_type("face_event")), 2, "默认关着，行为必须和以前一样")
        self.assertEqual(service.stats.suppressed, 0)

    def test_evidence_is_fed_every_frame_not_only_on_emit(self):
        """确认那一帧刚好侧脸（质量不入册）也要认得出来：参考库靠全程多帧建立。

        如果只在"要发事件的那一帧"才喂证据，这一次会留下空参考库 —— 人再回来就
        关联不上、又多报一条。这正是"每帧都 observe"的原因。
        """
        detector = ScriptedDetector()
        service, sink = build_assoc_service(
            self._tmp.name, ConstantRecognizer([0.0, 1.0, 0.0]), detector,
            {"enabled": True}, confirm_frames=3)
        step(service, detector, 0, [face_with_landmarks()])
        step(service, detector, 500, [face_with_landmarks()])
        step(service, detector, 1000, [face_with_landmarks(distort=200.0)])   # 确认帧质量差
        self.assertEqual(len(sink.of_type("face_event")), 1, "第一次在场要到确认帧才报")
        for ts in (1500, 2000, 2500, 3000, 3500):     # 离开超过轨道 TTL（2s）
            step(service, detector, ts, [])
        for ts in (4000, 4500, 5000):     # 重新确认需要 3 帧
            step(service, detector, ts, [face_with_landmarks(bbox=(104, 82, 80, 80))])
        self.assertEqual(len(sink.of_type("face_event")), 1)
        self.assertEqual(service.stats.suppressed, 1)

    def test_track_churn_hands_the_session_over_instead_of_reporting_again(self):
        """点位抖动导致 tracker 换轨（旧轨道 TTL 内还没死）：不能多报一条。

        这是 287 点回放里 12 条事件的根因之一：换轨时若拒绝交接，就会新建重复会话，
        之后同一个人同时像两条会话（own/other 都 0.85+），事件数反弹。
        """
        detector = ScriptedDetector()
        service, sink = build_assoc_service(
            self._tmp.name, ConstantRecognizer([0.0, 1.0, 0.0]), detector,
            {"enabled": True})
        for ts in (0, 500, 1000):
            step(service, detector, ts, [face_with_landmarks(bbox=(100, 80, 80, 80))])
        self.assertEqual(len(sink.of_type("face_event")), 1)
        # 框跳到完全不相交的位置 → tracker 建新轨道，旧轨道还在 TTL 内挂着
        jumped = face_with_landmarks(bbox=(420, 80, 80, 80))
        for ts in (1100, 1600, 2100):
            step(service, detector, ts, [jumped])
        self.assertEqual(service.tracker.track_count, 2, "旧轨道还没被 TTL 清掉")
        self.assertEqual(len(sink.of_type("face_event")), 1, "同一个人换轨不该多报")
        self.assertEqual(service.stats.suppressed, 1)

    def test_known_faces_are_not_suppressed_by_association(self):
        """身份去重是业务层（10 分钟到访）的事，视觉层不许替它做主。"""
        detector = ScriptedDetector()
        service, sink = build_assoc_service(
            self._tmp.name, ConstantRecognizer([1.0, 0.0, 0.0]), detector,
            {"enabled": True})
        known = face_with_landmarks()
        for ts in (0, 500, 1000):
            step(service, detector, ts, [known])
        for ts in (3000, 6000, 9000, 12000):        # 超过 10s 身份冷却
            step(service, detector, ts, [])
        for ts in (13000, 13500):
            step(service, detector, ts, [known])
        reported = sink.of_type("face_event")
        self.assertEqual(len(reported), 2, "已知人脸照旧按身份冷却上报")
        self.assertTrue(all(msg["is_known"] for msg in reported))
        self.assertEqual(service.stats.suppressed, 0)


class TestDuplicateDetectionGuard(unittest.TestCase):
    """同一帧同一张脸检出两次（幽灵框）：只报一条，也不能把会话复制成两份。"""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()

    def tearDown(self):
        self._tmp.cleanup()

    def test_overlapping_boxes_collapse_to_the_better_score(self):
        dets = [FaceDetection(100, 80, 100, 100, 0.91),
                FaceDetection(110, 90, 100, 100, 0.97)]     # IoU 约 0.68
        kept = VisionService._dedupe_detections(dets)
        self.assertEqual(len(kept), 1)
        self.assertAlmostEqual(kept[0].score, 0.97)

    def test_side_by_side_faces_are_both_kept(self):
        dets = [FaceDetection(100, 80, 100, 100, 0.95),
                FaceDetection(500, 80, 100, 100, 0.95)]     # 不相交
        self.assertEqual(len(VisionService._dedupe_detections(dets)), 2)

    def test_ghost_box_does_not_duplicate_the_session(self):
        detector = ScriptedDetector()
        service, sink = build_assoc_service(
            self._tmp.name, ConstantRecognizer([0.0, 1.0, 0.0]), detector,
            {"enabled": True})
        ghost = face_with_landmarks(bbox=(108, 86, 80, 80))   # 与主框 IoU > 0.5
        for ts in (0, 500, 1000):
            step(service, detector, ts, [face_with_landmarks(), ghost])
        self.assertEqual(len(sink.of_type("face_event")), 1, "一张脸只能一条事件")
        self.assertEqual(service.associator.session_count, 1, "不能复制出第二条会话")


class TestBackendLabel(unittest.TestCase):
    def test_label_marks_the_accelerator(self):
        class TrtDetector(FakeDetector):
            backend = "yunet-onnx"
            accelerator = "tensorrt"

        service, _sink = build_service(tempfile.mkdtemp(), detector=TrtDetector())
        self.assertEqual(service.backend_label, "yunet-onnx/tensorrt")

    def test_legacy_detector_label_is_just_the_name(self):
        service, _sink = build_service(tempfile.mkdtemp())
        self.assertEqual(service.backend_label, "fake-detector")

    def test_heartbeat_carries_the_backend(self):
        service, sink = build_service(tempfile.mkdtemp())
        service.run(duration_sec=1.0,
                    should_stop=lambda: bool(sink.of_type("vision_heartbeat")))
        beat = sink.of_type("vision_heartbeat")[0]
        self.assertEqual(beat["backend"], service.backend_label)


if __name__ == "__main__":
    unittest.main()
