"""Haar 同帧嵌套小框抑制（几何规则，纯函数）。

背景：Haar 在同一帧里经常既报「整张脸」，又报「脸内部的一小块」。跟踪器把
小框当成第二个人 → 同一帧多出一条轨道、多一次事件。本轮只修「嵌套」这一种
几何形态，不做通用 NMS，也不引入可配置阈值。

钉死的规则（docs/02 的检测阶段）：
1. 候选框按面积从大到小依次考虑（同面积按坐标排序，保证确定性）
2. 候选有效且正面积；无效宽高直接丢弃（不能除零）
3. 候选面积 <= 已保留大框面积 * 0.5，且 交集/候选面积 >= 0.9 → 抑制
4. 输出保持**输入顺序**，元素是**原对象**，不改入参

同样重要的是「不误伤」：相邻两张脸、同尺寸部分重叠、单独的小脸都必须保留。
这里只说明几何线索，不宣称凡是嵌套就一定误检。

测试不接摄像头：HaarDetector.detect 用假 classifier + `mock.patch` 驱动。
测试文件**正常 import 真 cv2 / vision.detector**（板端原生依赖）；本机缺 cv2 时
只用 logs/ 下的一次性外部 bootstrap 跑本文件，不向仓库测试代码里塞假包。
"""
import inspect
import json
import os
import types
import unittest
from unittest import mock

import numpy as np

import vision.detector as detector
from vision import config, events
from vision.detector import FaceDetection, HaarDetector
from vision.tracker import FaceTracker

FIXTURE = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                       "fixtures", "haar_nested_trace.json")

# 回放用统一基准时刻：fixture 里只有相对时戳，绝对时间由测试自己给。
BASE_MS = 1700000000000


def det(x, y, w, h, score=0.9):
    return FaceDetection(x, y, w, h, score)


def bbox_list(detections):
    return [d.bbox for d in detections]


def suppress(detections):
    """直接调生产函数。RED 阶段这里以 AttributeError 失败 = 缺函数证据。"""
    return detector.suppress_nested_detections(detections)


def face_dict(detection):
    return {
        "bbox": detection.bbox,
        "face_id": None,
        "name": None,
        "score": detection.score,
    }


class TestSuppressNested(unittest.TestCase):
    def test_nested_small_box_is_suppressed(self):
        large = det(470, 271, 258, 258)
        small = det(482, 349, 109, 109)   # 完全落在大框内部
        result = suppress([large, small])
        self.assertEqual(bbox_list(result), [(470, 271, 258, 258)])
        self.assertIs(result[0], large)

    def test_nested_is_suppressed_regardless_of_input_order(self):
        large = det(470, 271, 258, 258)
        small = det(482, 349, 109, 109)
        result = suppress([small, large])
        self.assertEqual(bbox_list(result), [(470, 271, 258, 258)])
        self.assertIs(result[0], large)

    def test_area_exactly_half_is_suppressed(self):
        large = det(0, 0, 100, 100)       # 10000
        small = det(0, 0, 100, 50)        # 5000 == 50%
        self.assertEqual(bbox_list(suppress([large, small])), [(0, 0, 100, 100)])

    def test_area_slightly_above_half_is_kept(self):
        large = det(0, 0, 200, 200)       # 40000，半面积 20000
        big_inner = det(0, 0, 142, 142)   # 20164 > 20000，仍在框内
        self.assertEqual(
            bbox_list(suppress([large, big_inner])),
            [(0, 0, 200, 200), (0, 0, 142, 142)],
        )

    def test_coverage_exactly_90_percent_is_suppressed(self):
        large = det(0, 0, 200, 200)       # 40000
        # 小框下沿超出大框 10px：交集 100x90 = 9000 → 恰好 90%
        small = det(0, 110, 100, 100)
        self.assertEqual(bbox_list(suppress([large, small])), [(0, 0, 200, 200)])

    def test_coverage_slightly_above_90_percent_is_suppressed(self):
        large = det(0, 0, 200, 200)
        small = det(0, 109, 100, 100)     # 交集 9100 → 91%
        self.assertEqual(bbox_list(suppress([large, small])), [(0, 0, 200, 200)])

    def test_coverage_slightly_below_90_percent_is_kept(self):
        large = det(0, 0, 200, 200)
        small = det(0, 111, 100, 100)     # 交集 8900 → 89%
        self.assertEqual(
            bbox_list(suppress([large, small])),
            [(0, 0, 200, 200), (0, 111, 100, 100)],
        )

    def test_multi_level_nesting_collapses_to_the_outer_box(self):
        outer = det(0, 0, 300, 300)
        middle = det(50, 50, 200, 200)
        inner = det(100, 100, 100, 100)
        self.assertEqual(
            bbox_list(suppress([inner, outer, middle])),
            [(0, 0, 300, 300)],
        )

    def test_empty_list_returns_empty_list(self):
        source = []
        result = suppress(source)
        self.assertEqual(result, [])
        self.assertIsInstance(result, list)
        self.assertIsNot(result, source)

    def test_single_box_returns_a_new_list_with_the_same_object(self):
        only = det(100, 100, 80, 80)
        source = [only]
        result = suppress(source)
        self.assertEqual(len(result), 1)
        self.assertIs(result[0], only)
        self.assertIsNot(result, source)
        self.assertEqual(source, [only])

    def test_invalid_width_height_are_dropped_without_zero_division(self):
        large = det(0, 0, 100, 100)
        zero_w = det(10, 10, 0, 80)
        negative_h = det(20, 20, 80, -5)
        negative_w = det(30, 30, -1, -1)
        self.assertEqual(bbox_list(suppress([zero_w, large, negative_h, negative_w])),
                         [(0, 0, 100, 100)])
        self.assertEqual(suppress([zero_w, negative_h, negative_w]), [])

    def test_input_list_and_objects_are_not_mutated(self):
        large = det(0, 0, 200, 200, 0.71)
        nested = det(20, 20, 90, 90, 0.62)
        standalone = det(500, 0, 60, 60, 0.55)
        source = [large, nested, standalone]
        snapshot = [d.bbox for d in source]
        result = suppress(source)
        self.assertEqual(source, [large, nested, standalone])
        self.assertEqual([d.bbox for d in source], snapshot)
        self.assertEqual([d.bbox for d in result],
                         [(0, 0, 200, 200), (500, 0, 60, 60)])

    def test_output_keeps_input_order(self):
        first = det(0, 0, 200, 200)
        second = det(500, 0, 60, 60)
        nested = det(10, 10, 50, 50)
        result = suppress([first, second, nested])
        self.assertIs(result[0], first)
        self.assertIs(result[1], second)

    def test_metadata_and_raw_row_survive(self):
        landmarks = [(1, 2), (3, 4), (5, 6), (7, 8), (9, 10)]
        raw_row = np.array([10, 10, 80, 80, 1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 0.66],
                           dtype=np.float32)
        kept = FaceDetection(10, 10, 80, 80, 0.66, landmarks=landmarks, raw_row=raw_row)
        nested = FaceDetection(20, 20, 30, 30, 0.42)
        result = suppress([kept, nested])
        self.assertEqual(len(result), 1)
        self.assertIs(result[0], kept)
        self.assertEqual(result[0].score, 0.66)
        self.assertEqual(result[0].landmarks, landmarks)
        self.assertIs(result[0].raw_row, raw_row)

    def test_same_size_partial_overlap_is_kept(self):
        a = det(100, 100, 80, 80)
        b = det(140, 140, 80, 80)   # 同尺寸、部分重叠：不是「嵌套小框」
        self.assertEqual(bbox_list(suppress([a, b])),
                         [(100, 100, 80, 80), (140, 140, 80, 80)])

    def test_duplicate_same_size_boxes_are_not_merged(self):
        a = det(100, 100, 80, 80)
        b = det(100, 100, 80, 80)   # 重复框不在本轮处理范围
        self.assertEqual(bbox_list(suppress([a, b])),
                         [(100, 100, 80, 80), (100, 100, 80, 80)])

    def test_adjacent_faces_are_kept(self):
        a = det(100, 100, 80, 80)
        b = det(180, 100, 80, 80)
        self.assertEqual(bbox_list(suppress([a, b])),
                         [(100, 100, 80, 80), (180, 100, 80, 80)])

    def test_standalone_small_face_is_kept(self):
        small = det(600, 400, 40, 40)
        self.assertEqual(bbox_list(suppress([small])), [(600, 400, 40, 40)])


def build_haar(raw_boxes, equalize=True):
    """__new__ 构造 HaarDetector + patch detector.cv2：不读真 XML、不碰摄像头。

    equalize 必须显式设置：生产实现里 detect 会读 self.equalize，旧测试若漏设
    会以 AttributeError 伪失败。
    """
    calls = {"equalize": [], "kwargs": None, "gray": None, "equalized": object()}

    class _Classifier(object):
        def empty(self):
            return False

        def detectMultiScale(self, gray, **kwargs):
            calls["kwargs"] = kwargs
            calls["gray"] = gray
            return list(raw_boxes)

    def _equalize(gray):
        calls["equalize"].append(gray)
        return calls["equalized"]

    fake_cv2 = types.SimpleNamespace(
        data=types.SimpleNamespace(haarcascades=""),
        CascadeClassifier=_Classifier,
        cvtColor=lambda frame, code: frame,
        equalizeHist=_equalize,
        COLOR_BGR2GRAY=6,
        CASCADE_SCALE_IMAGE=2,
    )
    impl = HaarDetector.__new__(HaarDetector)
    impl.cascade_path = "haarcascade_frontalface_default.xml"
    impl.min_face_px = 80
    impl.min_neighbors = 5
    impl.scale_factor = 1.1
    impl.equalize = equalize
    impl._cascade = _Classifier()
    patcher = mock.patch.object(detector, "cv2", fake_cv2)
    patcher.start()
    return impl, calls, patcher


class TestHaarDetectorIntegration(unittest.TestCase):
    """HaarDetector.detect 必须走抑制；用假 classifier，不碰摄像头。"""

    def _build(self, raw_boxes):
        impl, calls, patcher = build_haar(raw_boxes, equalize=True)
        self.addCleanup(patcher.stop)
        return impl, calls

    def test_detect_suppresses_nested_box(self):
        detector_impl, calls = self._build([
            (468, 259, 262, 262),
            (482, 349, 109, 109),
        ])
        frame = np.zeros((720, 1280, 3), dtype=np.uint8)
        result = detector_impl.detect(frame)
        self.assertEqual(bbox_list(result), [(468, 259, 262, 262)])
        self.assertEqual(result[0].score, 0.99)
        self.assertEqual(calls["kwargs"]["minSize"], (80, 80))

    def test_detect_keeps_adjacent_faces(self):
        detector_impl, _calls = self._build([
            (100, 100, 80, 80),
            (180, 100, 80, 80),
        ])
        result = detector_impl.detect(np.zeros((480, 640, 3), dtype=np.uint8))
        self.assertEqual(bbox_list(result),
                         [(100, 100, 80, 80), (180, 100, 80, 80)])

    def test_describe_mentions_the_nested_suppression_rule(self):
        detector_impl, _calls = self._build([])
        text = detector_impl.describe()
        self.assertIn("haar", text)
        self.assertIn("嵌套", text)
        self.assertIn("50%", text)
        self.assertIn("90%", text)


class TestHaarEqualizeSwitch(unittest.TestCase):
    """equalize 开关：False 走原 gray，True 走 equalizeHist 输出；两条分支都仍抑制嵌套。"""

    def _detect(self, raw_boxes, equalize):
        impl, calls, patcher = build_haar(raw_boxes, equalize=equalize)
        self.addCleanup(patcher.stop)
        frame = np.zeros((480, 640, 3), dtype=np.uint8)
        return impl, calls, frame, impl.detect(frame)

    def test_default_is_true_and_parameter_is_last(self):
        parameters = list(inspect.signature(HaarDetector.__init__).parameters.items())
        self.assertEqual(parameters[-1][0], "equalize", "新参数必须在末尾，保持位置兼容")
        self.assertIs(parameters[-1][1].default, True)

    def test_false_skips_equalize_and_feeds_original_gray(self):
        _impl, calls, frame, _result = self._detect([(100, 100, 80, 80)], equalize=False)
        self.assertEqual(calls["equalize"], [])
        self.assertIs(calls["gray"], frame)

    def test_true_equalizes_and_feeds_its_output(self):
        _impl, calls, frame, _result = self._detect([(100, 100, 80, 80)], equalize=True)
        self.assertEqual(len(calls["equalize"]), 1)
        self.assertIs(calls["equalize"][0], frame)
        self.assertIs(calls["gray"], calls["equalized"])

    def test_nested_suppression_still_runs_in_both_branches(self):
        nested = [(468, 259, 262, 262), (482, 349, 109, 109)]
        for equalize in (True, False):
            _impl, _calls, _frame, result = self._detect(nested, equalize=equalize)
            self.assertEqual(bbox_list(result), [(468, 259, 262, 262)],
                             "equalize=%r 时也必须走嵌套抑制" % equalize)

    def test_describe_reports_equalize_state_and_keeps_rule(self):
        for equalize in (True, False):
            impl, _calls, patcher = build_haar([], equalize=equalize)
            self.addCleanup(patcher.stop)
            text = impl.describe()
            self.assertIn("equalize=%s" % equalize, text)
            self.assertIn("嵌套", text)
            self.assertIn("50%", text)
            self.assertIn("90%", text)


class TestCreateDetectorHaarEqualize(unittest.TestCase):
    """create_detector 必须把配置里的 haar_equalize 传给 HaarDetector。"""

    def _cfg(self):
        cfg = config.load("sim")
        cfg["detection"]["backend"] = "haar"
        return cfg

    def _kwargs(self, cfg):
        with mock.patch.object(detector, "HaarDetector") as fake:
            detector.create_detector(cfg)
        return fake.call_args[1]

    def test_false_is_passed_through(self):
        cfg = self._cfg()
        cfg["detection"]["haar_equalize"] = False
        self.assertIs(self._kwargs(cfg).get("equalize"), False)

    def test_true_is_passed_through(self):
        cfg = self._cfg()
        cfg["detection"]["haar_equalize"] = True
        self.assertIs(self._kwargs(cfg).get("equalize"), True)

    def test_missing_key_defaults_true(self):
        cfg = self._cfg()
        del cfg["detection"]["haar_equalize"]
        self.assertIs(self._kwargs(cfg).get("equalize"), True)


class TestNonNestedFacesStillProduceTwoEvents(unittest.TestCase):
    """过滤后相邻双脸 / 同尺寸部分重叠仍是两个人 → 两条事件。"""

    def test_adjacent_faces(self):
        detections = suppress([det(100, 100, 80, 80), det(180, 100, 80, 80)])
        tracker = FaceTracker(confirm_frames=1)
        events_out = tracker.update([face_dict(d) for d in detections], 1000)
        self.assertEqual(len(events_out), 2)
        self.assertNotEqual(events_out[0].face_id, events_out[1].face_id)

    def test_same_size_partial_overlap(self):
        detections = suppress([det(100, 100, 80, 80), det(140, 140, 80, 80)])
        tracker = FaceTracker(confirm_frames=1)
        events_out = tracker.update([face_dict(d) for d in detections], 1000)
        self.assertEqual(len(events_out), 2)
        self.assertNotEqual(events_out[0].face_id, events_out[1].face_id)


class TestTrackTtl(unittest.TestCase):
    """短遮挡不重复上报；TTL 过期后重入是新轨道（不声称同一个人）。"""

    def _tracker(self):
        return FaceTracker(confirm_frames=3, cooldown_sec=10.0, track_ttl_sec=2.0)

    def test_short_occlusion_within_ttl_does_not_duplicate(self):
        tracker = self._tracker()
        first = []
        for ts in (0, 400, 800):
            first.extend(tracker.update([face_dict(det(100, 100, 80, 80))], ts))
        self.assertEqual(len(first), 1)
        # 遮挡 ≈1.2s，仍在 2s TTL 内 → 同一个已上报轨道，不产生新事件
        after = []
        after.extend(tracker.update([], 1200))
        after.extend(tracker.update([], 1600))
        for ts in (2000, 2400):
            after.extend(tracker.update([face_dict(det(100, 100, 80, 80))], ts))
        self.assertEqual(after, [])

    def test_ttl_expired_reentry_is_a_new_unknown_event(self):
        tracker = self._tracker()
        first = []
        for ts in (0, 400, 800):
            first.extend(tracker.update([face_dict(det(100, 100, 80, 80))], ts))
        self.assertEqual(len(first), 1)
        # 离开 > 2s TTL，轨道被清掉；再回来只能算「新的未知人脸」
        tracker.update([], 3000)
        self.assertEqual(tracker.track_count, 0)
        second = []
        for ts in (3200, 3600, 4000):
            second.extend(tracker.update([face_dict(det(100, 100, 80, 80))], ts))
        self.assertEqual(len(second), 1)
        self.assertNotEqual(second[0].face_id, first[0].face_id)
        self.assertFalse(second[0].is_known)
        self.assertIsNone(second[0].name)


def load_fixture():
    with open(FIXTURE, "r", encoding="utf-8") as fh:
        return json.load(fh)


def replay(frames, use_suppression):
    """按板端参数回放：confirm=3 / TTL=2s / cooldown=10s。

    fixture 只有相对时戳和 bbox；这里自己给基准时刻。返回
    ([(帧号, TrackEvent)...], {帧号: 抑制框数})。
    """
    tracker = FaceTracker(confirm_frames=3, cooldown_sec=10.0, track_ttl_sec=2.0)
    emitted = []
    suppressed = {}
    for index, frame in enumerate(frames, 1):
        detections = [det(x, y, w, h, 0.99) for (x, y, w, h) in frame["boxes"]]
        kept = suppress(detections) if use_suppression else detections
        suppressed[index] = len(detections) - len(kept)
        for event in tracker.update([face_dict(d) for d in kept],
                                    BASE_MS + int(frame["t_ms"])):
            emitted.append((index, event))
    return emitted, suppressed


class TestTraceFixtureReplay(unittest.TestCase):
    def setUp(self):
        self.fixture = load_fixture()
        self.frames = self.fixture["frames"]

    def test_fixture_contains_only_relative_time_and_bbox(self):
        self.assertEqual(sorted(self.fixture.keys()), ["frames"])
        self.assertEqual(len(self.frames), 40)
        stamps = []
        for frame in self.frames:
            self.assertEqual(sorted(frame.keys()), ["boxes", "t_ms"])
            self.assertIsInstance(frame["t_ms"], int)
            stamps.append(frame["t_ms"])
            self.assertTrue(frame["boxes"])
            for box in frame["boxes"]:
                self.assertEqual(len(box), 4)
                for value in box:
                    self.assertIsInstance(value, int)
        self.assertEqual(stamps[0], 0)
        self.assertEqual(stamps, sorted(stamps))
        self.assertLess(stamps[-1], 60000, "相对时戳不该是绝对时间")
        with open(FIXTURE, "r", encoding="utf-8") as fh:
            text = fh.read()
        for forbidden in ("unk_", "face_id", "1793449137160", ".jpg", ".png", "camera"):
            self.assertNotIn(forbidden, text)

    def test_unfiltered_replay_reproduces_two_events(self):
        emitted, _suppressed = replay(self.frames, use_suppression=False)
        self.assertEqual(len(emitted), 2)
        self.assertEqual([index for index, _event in emitted], [3, 7])

    def test_filtered_replay_leaves_one_event_on_the_third_frame(self):
        emitted, suppressed = replay(self.frames, use_suppression=True)
        self.assertEqual(len(emitted), 1)
        index, event = emitted[0]
        self.assertEqual(index, 3, "第 3 帧首次触发")
        self.assertEqual(event.frame_ts, BASE_MS + 903)
        self.assertEqual(sum(suppressed.values()), 3, "共抑制 3 框")
        self.assertEqual(
            dict((i, n) for i, n in suppressed.items() if n),
            {5: 1, 6: 1, 22: 1},
        )

    def test_filtered_event_passes_the_protocol_check(self):
        emitted, _suppressed = replay(self.frames, use_suppression=True)
        self.assertEqual(len(emitted), 1)
        _index, event = emitted[0]
        msg = events.face_event(
            event.face_id, event.is_known, event.name, event.confidence,
            bbox=event.bbox, frame_ts=event.frame_ts,
        )
        self.assertEqual(events.check(msg), [])
        self.assertEqual(msg["bbox"], [513, 262, 261, 261])
        self.assertFalse(msg["is_known"])
        self.assertIsNone(msg["name"])


if __name__ == "__main__":
    unittest.main()
