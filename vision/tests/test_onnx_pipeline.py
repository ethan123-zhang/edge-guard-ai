"""ONNX 管线的接线测试：不需要真模型、不需要 TensorRT。

钉住的是**我们自己的接线**，不是模型精度：

- 检测：320 blob 的输出怎么缩放回原图、关键点顺序、raw_row 是否可直接喂对齐；
- 识别：批量分块（engine max=8）、L2 归一化、对不齐的脸返回 None 且位置不变；
- 回退：`auto` 下 engine 不可用退 CPU dnn；`trt` 下不可用**必须报错**而不是偷偷降级；
- 预热：检测和识别各跑一次，且识别按最大批量预热。

真模型的数值验收在板端脚本里（`logs/video-validation/pipeline_board_check.py`）。
"""
import os
import tempfile
import unittest
from unittest import mock

import numpy as np

from vision import align, config, onnx_pipeline
from vision.detector import FaceDetection
from vision.yunet_decode import PRIOR_COUNT


class FakeRunner(object):
    """按脚本返回固定输出，并记录每次输入形状。默认行为像 engine（一次能吃 8 张）。"""

    kind = "fake"
    max_batch = 8

    def __init__(self, outputs, name="fake.onnx", max_batch=8):
        self._outputs = outputs
        self.name = name
        self.max_batch = max_batch
        self.calls = []

    def run(self, tensor):
        self.calls.append(tuple(tensor.shape))
        if callable(self._outputs):
            return self._outputs(tensor)
        return self._outputs

    def describe(self):
        return "fake(%s)" % self.name


def detection_outputs(hits, count=PRIOR_COUNT):
    """构造 YuNet 三路输出，`hits` 是 {先验序号: (conf, overlap)}。"""
    loc = np.zeros((count, 14), dtype=np.float32)
    conf = np.zeros((count, 2), dtype=np.float32)
    overlap = np.zeros(count, dtype=np.float32)
    for index, (c, o) in hits.items():
        conf[index, 1] = c
        overlap[index] = o
    return [loc, conf, overlap]


def embed_outputs(count):
    """SFace 输出：第 i 张脸的第 i 维为 1（后面再乘一个非单位长度验证归一化）。"""
    matrix = np.zeros((count, 128), dtype=np.float32)
    for index in range(count):
        matrix[index, index % 128] = 3.0
    return [matrix]


def make_detection(raw_points=None):
    points = np.asarray(raw_points if raw_points is not None else align.ALIGN_TARGET,
                        dtype=np.float32)
    raw = np.zeros(15, dtype=np.float32)
    raw[0:4] = (10, 20, 50, 50)
    raw[4:14] = points.reshape(-1)
    raw[14] = 0.95
    return FaceDetection(10, 20, 50, 50, 0.95,
                         [(float(x), float(y)) for x, y in points], raw_row=raw)


class TestOnnxDetector(unittest.TestCase):
    def test_scales_rows_back_to_frame_coordinates(self):
        # 先验 0 = (4, 4, 10, 10)；命中后框中心约 (4,4)，320 → 640x480 要翻倍
        runner = FakeRunner(detection_outputs({0: (1.0, 1.0)}))
        detector = onnx_pipeline.OnnxYunetDetector(runner, score_threshold=0.6)
        frame = np.zeros((480, 640, 3), dtype=np.uint8)
        found = detector.detect(frame)
        self.assertEqual(len(found), 1)
        face = found[0]
        self.assertEqual(runner.calls, [(1, 3, 320, 320)])
        # 中心 x≈4/320*640=8, y≈4/320*480=6
        self.assertAlmostEqual(face.x + face.w / 2.0, 8.0, delta=1.0)
        self.assertAlmostEqual(face.y + face.h / 2.0, 6.0, delta=1.0)
        self.assertEqual(len(face.landmarks), 5)
        self.assertEqual(face.raw_row.shape, (15,))

    def test_landmarks_are_scaled_with_the_box(self):
        runner = FakeRunner(detection_outputs({0: (1.0, 1.0)}))
        detector = onnx_pipeline.OnnxYunetDetector(runner, score_threshold=0.6)
        frame = np.zeros((640, 640, 3), dtype=np.uint8)
        face = detector.detect(frame)[0]
        # 先验 0 的关键点偏移为 0 → 五个点都落在先验中心 (4,4)，放大 2 倍后 (8,8)
        for x, y in face.landmarks:
            self.assertAlmostEqual(x, 8.0, delta=1.0)
            self.assertAlmostEqual(y, 8.0, delta=1.0)

    def test_score_threshold_comes_from_config(self):
        # score = sqrt(0.49) = 0.7：0.6 该留，0.9 该丢
        outputs = detection_outputs({2: (0.49, 1.0)})
        low = onnx_pipeline.OnnxYunetDetector(FakeRunner(outputs), score_threshold=0.6)
        high = onnx_pipeline.OnnxYunetDetector(FakeRunner(outputs), score_threshold=0.9)
        frame = np.zeros((320, 320, 3), dtype=np.uint8)
        self.assertEqual(len(low.detect(frame)), 1)
        self.assertEqual(len(high.detect(frame)), 0)

    def test_no_faces_returns_empty(self):
        detector = onnx_pipeline.OnnxYunetDetector(
            FakeRunner(detection_outputs({})), score_threshold=0.6)
        self.assertEqual(detector.detect(np.zeros((240, 320, 3), dtype=np.uint8)), [])


class TestOnnxRecognizer(unittest.TestCase):
    def test_returns_normalised_vectors_in_input_order(self):
        runner = FakeRunner(embed_outputs(2))
        recognizer = onnx_pipeline.OnnxSfaceRecognizer(runner, batch_limit=8)
        frame = np.zeros((128, 128, 3), dtype=np.uint8)
        vectors = recognizer.embed_batch(frame, [make_detection(), make_detection()])
        self.assertEqual(len(vectors), 2)
        for vector in vectors:
            self.assertAlmostEqual(float(np.linalg.norm(vector)), 1.0, places=5)

    def test_detections_without_raw_row_stay_none_in_place(self):
        runner = FakeRunner(embed_outputs(1))
        recognizer = onnx_pipeline.OnnxSfaceRecognizer(runner, batch_limit=8)
        frame = np.zeros((128, 128, 3), dtype=np.uint8)
        plain = FaceDetection(0, 0, 40, 40, 0.9)   # Haar 那种没有关键点的框
        vectors = recognizer.embed_batch(frame, [plain, make_detection()])
        self.assertIsNone(vectors[0])
        self.assertIsNotNone(vectors[1])

    def test_batches_are_chunked_to_engine_limit(self):
        runner = FakeRunner(lambda tensor: embed_outputs(tensor.shape[0]))
        recognizer = onnx_pipeline.OnnxSfaceRecognizer(runner, batch_limit=8)
        frame = np.zeros((128, 128, 3), dtype=np.uint8)
        vectors = recognizer.embed_batch(frame, [make_detection() for _ in range(10)])
        self.assertEqual(runner.calls, [(8, 3, 112, 112), (2, 3, 112, 112)])
        self.assertEqual(len(vectors), 10)
        self.assertTrue(all(v is not None for v in vectors))

    def test_single_embed_matches_batch_element(self):
        runner = FakeRunner(lambda tensor: embed_outputs(tensor.shape[0]))
        recognizer = onnx_pipeline.OnnxSfaceRecognizer(runner, batch_limit=8)
        frame = np.zeros((128, 128, 3), dtype=np.uint8)
        one = recognizer.embed(frame, make_detection())
        many = recognizer.embed_batch(frame, [make_detection()])[0]
        self.assertTrue(np.array_equal(one, many))

    def test_one_face_at_a_time_when_runner_cannot_batch(self):
        """CPU dnn 声明 max_batch=1：哪怕配置写 8，也只能一张一张喂。"""
        runner = FakeRunner(lambda tensor: embed_outputs(tensor.shape[0]), max_batch=1)
        recognizer = onnx_pipeline.OnnxSfaceRecognizer(runner, batch_limit=8)
        self.assertEqual(recognizer.batch_limit, 1)
        frame = np.zeros((128, 128, 3), dtype=np.uint8)
        recognizer.embed_batch(frame, [make_detection() for _ in range(3)])
        self.assertEqual(runner.calls, [(1, 3, 112, 112)] * 3)

    def test_wrong_embedding_dimension_is_loud(self):
        runner = FakeRunner([np.zeros((1, 64), dtype=np.float32)])
        recognizer = onnx_pipeline.OnnxSfaceRecognizer(runner, batch_limit=8)
        frame = np.zeros((128, 128, 3), dtype=np.uint8)
        with self.assertRaises(onnx_pipeline.PipelineUnavailable):
            recognizer.embed_batch(frame, [make_detection()])


class TestRunnerSelection(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.engine = os.path.join(self._tmp.name, "det.engine")
        with open(self.engine, "wb") as handle:
            handle.write(b"not a real engine")
        self.logs = []
        # 真模型加载在单测里既慢又依赖仓库路径（中文路径 cv2 读不了），
        # 这里只验证"选择逻辑"，把 dnn runner 换成假的。
        patcher = mock.patch.object(onnx_pipeline, "_make_dnn_runner",
                                    lambda model_path, outputs: FakeRunner(
                                        [], name=os.path.basename(model_path)))
        patcher.start()
        self.addCleanup(patcher.stop)

    def _cfg(self, backend):
        cfg = config.load("sim")
        cfg["runtime"]["backend"] = backend
        cfg["runtime"]["warmup"] = False
        return cfg

    def test_auto_falls_back_to_dnn_when_engine_unavailable(self):
        calls = []

        def no_engine(engine_path, model_path, log=None):
            calls.append(engine_path)
            return None

        cfg = self._cfg("auto")
        cfg["runtime"]["detector_engine"] = self.engine
        cfg["runtime"]["recognizer_engine"] = self.engine
        detector, _recognizer = onnx_pipeline.create_pipeline(
            cfg, log=self.logs.append, engine_factory=no_engine)
        self.assertEqual(len(calls), 2, "检测和识别各自尝试一次 engine")
        self.assertEqual(detector._runner.kind, "fake", "应当回退到 dnn")
        self.assertTrue(any("回退 CPU dnn" in line for line in self.logs),
                        "回退必须有日志，不能静默：%r" % (self.logs,))

    def test_trt_backend_raises_instead_of_silently_falling_back(self):
        cfg = self._cfg("trt")
        cfg["runtime"]["detector_engine"] = self.engine
        with self.assertRaises(onnx_pipeline.PipelineUnavailable) as caught:
            onnx_pipeline.create_pipeline(cfg, log=self.logs.append,
                                          engine_factory=lambda *a, **k: None)
        self.assertIn("trt", str(caught.exception))

    def test_cpu_backend_ignores_configured_engines(self):
        cfg = self._cfg("cpu")
        cfg["runtime"]["detector_engine"] = self.engine

        def exploding_factory(*args, **kwargs):
            raise AssertionError("cpu 模式不该去碰 engine")

        detector, recognizer = onnx_pipeline.create_pipeline(
            cfg, log=self.logs.append, engine_factory=exploding_factory)
        self.assertEqual(detector.backend, "yunet-onnx")
        self.assertEqual(recognizer.backend, "sface-onnx")

    def test_batch_clamp_is_logged_when_dnn_cannot_batch(self):
        cpu_runner = FakeRunner([], name="emb.onnx", max_batch=1)
        with mock.patch.object(onnx_pipeline, "_make_dnn_runner",
                               lambda model_path, outputs: cpu_runner):
            detector, recognizer = onnx_pipeline.create_pipeline(
                self._cfg("cpu"), log=self.logs.append)
        self.assertEqual(recognizer.batch_limit, 1)
        self.assertTrue(any("钳到 1" in line for line in self.logs),
                        "批量被钳掉必须留日志：%r" % (self.logs,))

    def test_legacy_backend_is_rejected(self):
        with self.assertRaises(onnx_pipeline.PipelineUnavailable):
            onnx_pipeline.create_pipeline(self._cfg("legacy"), log=self.logs.append)


class TestWarmup(unittest.TestCase):
    def test_warmup_hits_detector_and_full_recognizer_batch(self):
        detect_runner = FakeRunner(lambda tensor: detection_outputs({}))
        embed_runner = FakeRunner(lambda tensor: embed_outputs(tensor.shape[0]))
        detector = onnx_pipeline.OnnxYunetDetector(detect_runner, score_threshold=0.6)
        recognizer = onnx_pipeline.OnnxSfaceRecognizer(embed_runner, batch_limit=8)
        onnx_pipeline._warmup(detector, recognizer)
        self.assertEqual(detect_runner.calls, [(1, 3, 320, 320)])
        self.assertEqual(embed_runner.calls, [(8, 3, 112, 112)])


if __name__ == "__main__":
    unittest.main()