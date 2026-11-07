"""裸 ONNX / TensorRT 的检测+识别管线：板上也能跑 YuNet + SFace。

**为什么需要这条路径**：板上 OpenCV 4.1.1 没有 `cv2.FaceDetectorYN` 和
`cv2.FaceRecognizerSF`，生产一直走 Haar（没有关键点 → 只能报陌生人）。而 YuNet 与
SFace 的 ONNX 本身能跑 `cv2.dnn`，TensorRT 也在板上装着。于是这里把「检测 → 五点对齐
→ 特征」整个重写成自持的一段：检测用 `yunet_decode`，对齐用 `vision.align`，
SFace 按批喂。

**三条硬约定**：

1. **逐组件回退**：检测和识别各自独立判断 engine 能不能用（`trt_backend` 会核对
   engine/模型哈希与 TensorRT 版本）。`auto` 下哪个不可用哪个退 CPU dnn；
   `trt` 下任一不可用就抛 `PipelineUnavailable`，绝不静默降级。
2. **阈值跟配置走**：检测分数门槛用 `detection.score_threshold`（默认 0.6），
   与 `cv2.FaceDetectorYN` 语义一致；解码器的 0.9 默认值只是 P0 parity 口径。
3. **批上限**：SFace engine 的 profile 是 1..8，`recognizer_batch` 超过 8 要分块；
   分块是这里做的，调用方只管一次给一帧的所有脸。**CPU dnn 恒按 1 张喂**：板上
   OpenCV 4.1.1 的批量前向会把同一批的脸特征塌成同一个（见 `DnnRunner`），
   所以批量只是 TensorRT 的收益。

板端 Python 3.6：不用 dataclasses / 海象运算符 / `from __future__`。
"""
import os

import cv2
import numpy as np

from vision import align, config, detector, recognizer, trt_backend, yunet_decode
from vision.detector import FaceDetection

# engine / ONNX 的输入输出名（与 logs/video-validation/trt_regression.py 一致）
DETECT_INPUT = "input"
DETECT_OUTPUTS = ("loc", "conf", "iou")
EMBED_INPUT = "data"
EMBED_OUTPUTS = ("fc1",)

DETECT_SIZE = (320, 320)   # YuNet 固定 320（ONNX 里声明的 120x160 是历史遗留）
EMBED_SIZE = (112, 112)
EMBED_DIM = 128
DEFAULT_BATCH = 8

BACKENDS = ("legacy", "auto", "cpu", "trt")


class PipelineUnavailable(RuntimeError):
    """模型/engine/OpenCV 能力不满足。`auto` 调用方应回退旧路径；`trt` 直接报错。"""


class DnnRunner(object):
    """`cv2.dnn` 前向：PC 与板端 OpenCV 4.1.1 都能用，但没有加速。

    `max_batch = 1` 不是保守，是**实测**：板上 OpenCV 4.1.1 对 SFace 图做批量前向时，
    会把同一批里所有脸的特征塌成同一个向量（批量 8 张、8 行两两余弦全等于 1），
    而且状态会粘住 —— 之后再喂单张也回不到原来的特征（2026-11-07 板上复现，
    `logs/video-validation/debug_sface_batch_board.py`）。所以 CPU 路径恒按 1 张喂。
    """

    kind = "dnn"
    max_batch = 1

    def __init__(self, model_path, outputs=None):
        self.model_path = str(model_path)
        self._outputs = tuple(outputs) if outputs else None
        self._net = cv2.dnn.readNetFromONNX(self.model_path)

    def run(self, tensor):
        self._net.setInput(tensor)
        if self._outputs is None:
            raw = [self._net.forward()]
        else:
            raw = list(self._net.forward(list(self._outputs)))
        return [np.asarray(item, dtype=np.float32) for item in raw]

    def describe(self):
        return "dnn(%s)" % os.path.basename(self.model_path)


class TrtRunner(object):
    """TensorRT engine 前向；哈希/sidecar/版本核对交给 `trt_backend`。"""

    kind = "tensorrt"
    max_batch = DEFAULT_BATCH      # engine profile 上限就是 8

    def __init__(self, engine_path, model_path, input_name, outputs, log=None,
                 engine_factory=None):
        factory = engine_factory or trt_backend.create_backend
        backend = factory(engine_path, model_path, log=log)
        if backend is None:
            raise PipelineUnavailable("engine 不可用：%s" % engine_path)
        self._backend = backend
        self._input_name = input_name
        self._outputs = tuple(outputs)
        self.engine_path = str(engine_path)

    def run(self, tensor):
        out = self._backend.run({self._input_name: tensor})
        return [np.asarray(out[name], dtype=np.float32) for name in self._outputs]

    def close(self):
        self._backend.close()

    def describe(self):
        return "tensorrt(%s)" % os.path.basename(self.engine_path)


def _make_dnn_runner(model_path, outputs):
    try:
        return DnnRunner(model_path, outputs)
    except cv2.error as exc:
        raise PipelineUnavailable("ONNX 加载失败：%s（%s）" % (model_path, exc))


def _pick_runner(engine_path, model_path, input_name, outputs, backend, label,
                 log, engine_factory):
    """按配置选 engine 还是 dnn；`trt` 模式下 engine 不可用就直接报错。"""
    wants_engine = backend in ("auto", "trt")
    if wants_engine and engine_path and os.path.exists(engine_path):
        try:
            runner = TrtRunner(engine_path, model_path, input_name, outputs,
                               log=log, engine_factory=engine_factory)
            log("[vision] %s走 TensorRT：%s" % (label, os.path.basename(engine_path)))
            return runner
        except (PipelineUnavailable, trt_backend.BackendUnavailable) as exc:
            if backend == "trt":
                raise PipelineUnavailable("%s配置要求 trt，但 engine 不可用：%s" % (label, exc))
            log("[vision] %s的 TensorRT 不可用，回退 CPU dnn：%s" % (label, exc))
    elif backend == "trt":
        raise PipelineUnavailable("%s配置要求 trt，但没有可用的 engine 文件：%r"
                                  % (label, engine_path or "（未配置）"))
    runner = _make_dnn_runner(model_path, outputs)
    log("[vision] %s走 CPU dnn：%s" % (label, os.path.basename(model_path)))
    return runner


class OnnxYunetDetector(object):
    """YuNet ONNX 检测：固定 320 blob → 解码 → 缩放回原图坐标。"""

    backend = "yunet-onnx"

    def __init__(self, runner, score_threshold=0.6, min_face_px=80, input_size=DETECT_SIZE):
        self._runner = runner
        self._score_threshold = float(score_threshold)
        self._min_face_px = int(min_face_px)
        self._input_size = (int(input_size[0]), int(input_size[1]))
        # 心跳里要报「实际在跑什么」：tensorrt / dnn
        self.accelerator = runner.kind

    def detect(self, frame):
        height, width = frame.shape[:2]
        blob = cv2.dnn.blobFromImage(frame, 1.0, self._input_size)
        outputs = self._runner.run(blob)
        rows = yunet_decode.decode(outputs, score_threshold=self._score_threshold)
        scale_x = width / float(self._input_size[0])
        scale_y = height / float(self._input_size[1])
        detections = []
        for row in rows:
            # 拷贝一份再缩放：`rows` 的元素来自解码器，不要原地改后被别人复用
            scaled = np.array(row, dtype=np.float32)
            scaled[:14:2] *= scale_x
            scaled[1:14:2] *= scale_y
            marks = [(float(scaled[4 + 2 * index]), float(scaled[5 + 2 * index]))
                     for index in range(5)]
            detections.append(FaceDetection(
                scaled[0], scaled[1], scaled[2], scaled[3], float(scaled[14]),
                marks, raw_row=scaled,
            ))
        return detections

    def describe(self):
        return "yunet-onnx(%s, score>=%.2f)" % (self._runner.describe(), self._score_threshold)


class OnnxSfaceRecognizer(object):
    """SFace ONNX 识别：五点对齐 → 按批前向 → L2 归一化。"""

    backend = "sface-onnx"
    available = True

    def __init__(self, runner, batch_limit=DEFAULT_BATCH):
        self._runner = runner
        self.accelerator = runner.kind
        # runner 说一次能吃几张就吃几张：CPU dnn 只会点头 1 张（4.1.1 批量会把特征塌成
        # 同一个），TensorRT 才允许到 8。调用方的 `recognizer_batch` 只能往下钳。
        limit = getattr(runner, "max_batch", 1)
        self._batch_limit = max(1, min(int(batch_limit), int(limit)))

    @property
    def batch_limit(self):
        return self._batch_limit

    def embed(self, frame, detection):
        return self.embed_batch(frame, [detection])[0]

    def embed_batch(self, frame, detections):
        """返回与 `detections` 等长的列表，元素是归一化特征或 None（对不齐）。"""
        results = [None] * len(detections)
        crops = []
        slots = []
        for index, detection in enumerate(detections):
            crop = self._aligned_crop(frame, detection)
            if crop is None:
                continue
            slots.append(index)
            crops.append(crop)
        if not crops:
            return results
        vectors = self._embed_crops(crops)
        for slot, vector in zip(slots, vectors):
            results[slot] = vector
        return results

    @staticmethod
    def _aligned_crop(frame, detection):
        raw = getattr(detection, "raw_row", None)
        if raw is None:
            return None
        points = np.asarray(raw, dtype=np.float32).reshape(-1)
        if points.size < 14:
            return None
        return align.crop(frame, points[4:14].reshape(5, 2))

    def _embed_crops(self, crops):
        vectors = []
        for start in range(0, len(crops), self._batch_limit):
            chunk = crops[start:start + self._batch_limit]
            tensor = cv2.dnn.blobFromImages(chunk, 1.0, EMBED_SIZE, (0, 0, 0), swapRB=True)
            raw = self._runner.run(tensor)[0]
            matrix = np.asarray(raw, dtype=np.float32).reshape(len(chunk), -1)
            if matrix.shape[1] != EMBED_DIM:
                raise PipelineUnavailable(
                    "SFace 输出维度是 %d，期望 %d（engine 和模型对不上？）"
                    % (matrix.shape[1], EMBED_DIM)
                )
            for vector in matrix:
                norm = float(np.linalg.norm(vector))
                vectors.append(None if norm <= 1e-6 else (vector / norm))
        return vectors

    def describe(self):
        return "sface-onnx(%s, batch<=%d)" % (self._runner.describe(), self._batch_limit)


def _warmup(detector, recognizer, log=None):
    """跑一次空白图：engine 初始化、上下文/缓冲分配都在启动时付掉，别留到第一帧。

    识别侧按**最大批量**预热：TRT 的缓冲按实际形状分配，先按 8 分配一次，
    之后小批量直接复用；反过来先跑 1 张、真来 6 张时还得重新分配。
    """
    frame = np.zeros((DETECT_SIZE[1], DETECT_SIZE[0], 3), dtype=np.uint8)
    detector.detect(frame)
    marks = np.array(align.ALIGN_TARGET, dtype=np.float32).reshape(-1)
    raw = np.zeros(15, dtype=np.float32)
    raw[4:14] = marks
    warm = FaceDetection(0, 0, EMBED_SIZE[0], EMBED_SIZE[1], 1.0,
                         [(float(x), float(y)) for x, y in align.ALIGN_TARGET], raw_row=raw)
    recognizer.embed_batch(frame, [warm] * int(recognizer.batch_limit))
    if log is not None:
        log("[vision] 管线预热完成")


def create_pipeline(cfg, log=None, engine_factory=None):
    """按 `runtime.backend` 造 `(detector, recognizer)`。

    `legacy` 不该走到这里（调用方用旧的 `detector/recognizer` 工厂）。
    """
    log = log or (lambda message: None)
    runtime = cfg["runtime"]
    backend = str(runtime["backend"])
    if backend == "legacy":
        raise PipelineUnavailable("runtime.backend=legacy 走旧路径，不该建 ONNX 管线")
    if backend not in BACKENDS:
        raise PipelineUnavailable("runtime.backend=%r 不合法，可选 %s" % (backend, BACKENDS))

    detect_model = config.resolve_vision(runtime["detector_onnx"])
    embed_model = config.resolve_vision(cfg["recognition"]["model"])
    factory = engine_factory or trt_backend.create_backend

    detect_runner = _pick_runner(
        _resolve_optional(runtime.get("detector_engine")), detect_model,
        DETECT_INPUT, DETECT_OUTPUTS, backend, "检测", log, factory,
    )
    embed_runner = _pick_runner(
        _resolve_optional(runtime.get("recognizer_engine")), embed_model,
        EMBED_INPUT, EMBED_OUTPUTS, backend, "识别", log, factory,
    )

    detector = OnnxYunetDetector(
        detect_runner,
        score_threshold=cfg["detection"]["score_threshold"],
        min_face_px=cfg["detection"]["min_face_px"],
    )
    recognizer = OnnxSfaceRecognizer(
        embed_runner, batch_limit=runtime.get("recognizer_batch", DEFAULT_BATCH),
    )
    requested = int(runtime.get("recognizer_batch", DEFAULT_BATCH))
    if recognizer.batch_limit < requested:
        log("[vision] 识别批量从 %d 钳到 %d：%s 一次只可靠地喂 %d 张"
            % (requested, recognizer.batch_limit, embed_runner.kind,
               embed_runner.max_batch))
    if bool(runtime.get("warmup", True)):
        _warmup(detector, recognizer, log=log)
    return detector, recognizer


def _resolve_optional(path):
    """engine 路径可空（空=没有 engine）；给相对路径时按 `vision/` 解析。"""
    text = "" if path is None else str(path).strip()
    if not text:
        return ""
    return config.resolve_vision(text)


def create_components(cfg, log=None):
    """按 `runtime.backend` 造 `(detector, recognizer, mode)`，供入口脚本统一调用。

    - `legacy`：旧路径（`cv2.FaceDetectorYN` / Haar + `cv2.FaceRecognizerSF`），行为不变；
    - `auto`：优先 ONNX 管线（engine 可用则 TRT，否则 CPU dnn）；**建不起来就退回
      legacy 并大声打日志**，不让一个可选项把整个节点拖死；
    - `cpu` / `trt`：显式选择，失败直接抛 `PipelineUnavailable`（写明了就别偷偷降级）。

    `mode` 是 `"onnx"` 或 `"legacy"`，给调用方打日志/做后续判断用。
    """
    log = log or (lambda message: None)
    backend = str(cfg["runtime"]["backend"])

    def legacy():
        det = detector.create_detector(cfg, log=log)
        try:
            rec = recognizer.create_recognizer(cfg, log=log)
        except recognizer.RecognizerUnavailable as exc:
            log("[vision] 识别器不可用，降级为 none：%s" % exc)
            rec = recognizer.NullRecognizer()
        return det, rec, "legacy"

    if backend == "legacy":
        return legacy()
    try:
        detect, embed = create_pipeline(cfg, log=log)
        return detect, embed, "onnx"
    except PipelineUnavailable as exc:
        if backend in ("cpu", "trt"):
            raise
        log("[vision] 警告：ONNX 管线不可用，回退旧路径：%s" % exc)
        return legacy()