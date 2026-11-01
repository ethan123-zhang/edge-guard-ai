"""人脸检测：YuNet（准，要模型） + Haar（离线兜底，不用模型）。

**为什么要兜底**：模型文件 230KB，但当前开发机网络策略把下载全挡了（403）。
没有模型时如果直接崩，整条链路就验证不了 —— 所以 `backend: auto` 的策略是
「有模型用 YuNet，没有就用 Haar」，并在日志里明说降级了，不装哑巴。

Haar 的已知缺陷（必须写清楚，别指望它）：**没有 5 点关键点**，所以
SFace 无法对齐 → 走 Haar 时识别只能给"陌生人"。这也是为什么一拿到模型就要切回 YuNet。
"""
import os

import cv2
import numpy as np

from vision import config


# 嵌套小框抑制的固定几何阈值：只修 Haar「大框里又报一个小框」这一种形态，
# 不是通用 NMS，也不外置成配置项（本轮明确不扩大范围）。
NESTED_AREA_RATIO = 0.5
NESTED_COVERAGE_RATIO = 0.9


class DetectorUnavailable(RuntimeError):
    """模型/级联文件缺失或加载失败。消息里要给出"下一步该干什么"。"""


class FaceDetection(object):
    """一张检测到的人脸。坐标是**传入那一帧**的像素坐标。"""

    __slots__ = ("x", "y", "w", "h", "score", "landmarks", "raw_row")

    def __init__(self, x, y, w, h, score=0.0, landmarks=None, raw_row=None):
        self.x = int(x)
        self.y = int(y)
        self.w = int(w)
        self.h = int(h)
        self.score = float(score)
        # 5 个关键点 [(x,y) * 5]：右眼、左眼、鼻尖、右嘴角、左嘴角
        self.landmarks = landmarks
        # YuNet 的原始 15 列输出，SFace 的 alignCrop 直接吃它
        self.raw_row = raw_row

    @property
    def bbox(self):
        return (self.x, self.y, self.w, self.h)

    @property
    def size(self):
        return min(self.w, self.h)

    def shifted(self, dx, dy):
        """裁剪 ROI 后把人脸坐标平移回整帧坐标（快照标注要用整帧坐标）。"""
        return FaceDetection(
            self.x + dx, self.y + dy, self.w, self.h, self.score,
            landmarks=None if self.landmarks is None
            else [(px + dx, py + dy) for px, py in self.landmarks],
            raw_row=self.raw_row,
        )

    def to_dict(self):
        return {"bbox": list(self.bbox), "score": round(self.score, 3)}

    def __repr__(self):
        return "FaceDetection(bbox=%s, score=%.3f)" % (self.bbox, self.score)


def _intersection_area(box_a, box_b):
    """两个 (x, y, w, h) 的交集面积；不相交返回 0。"""
    ax, ay, aw, ah = box_a
    bx, by, bw, bh = box_b
    inter_w = max(0, min(ax + aw, bx + bw) - max(ax, bx))
    inter_h = max(0, min(ay + ah, by + bh) - max(ay, by))
    return inter_w * inter_h


def suppress_nested_detections(detections):
    """压掉同帧里「几乎完全套在已保留大框内」的小框（Haar 常见重复响应）。

    规则固定，不做通用 NMS：
    - 只考虑有效且正面积的框（w/h <= 0 直接丢弃，避免除零）
    - 按面积从大到小处理；同面积按坐标排序，保证结果确定
    - 候选面积 <= 已保留大框面积 * 0.5，且 交集/候选面积 >= 0.9 → 抑制
    - 返回值是**新列表**、按**输入顺序**排列、元素是**入参原对象**；
      入参列表和对象都不改

    重复的相同尺寸框不在本轮处理范围；相邻双脸、同尺寸部分重叠、单独的小脸
    都会被保留。这里只说明几何线索，不断言嵌套框一定是误检。
    """
    candidates = []
    for index, item in enumerate(detections):
        width, height = item.w, item.h
        if width <= 0 or height <= 0:
            continue
        # (输入序号, 面积, x, y, w, h)：后四项只用于确定性排序
        candidates.append((index, width * height, item.x, item.y, width, height))
    candidates.sort(key=lambda entry: (-entry[1], entry[2], entry[3], entry[4], entry[5]))

    kept_indices = set()
    kept_boxes = []   # (面积, (x, y, w, h))，只含已保留的大框
    for index, area, x, y, width, height in candidates:
        box = (x, y, width, height)
        nested = False
        for kept_area, kept_box in kept_boxes:
            if area > kept_area * NESTED_AREA_RATIO:
                continue
            coverage = _intersection_area(box, kept_box) / float(area)
            if coverage >= NESTED_COVERAGE_RATIO:
                nested = True
                break
        if not nested:
            kept_indices.add(index)
            kept_boxes.append((area, box))

    return [item for index, item in enumerate(detections) if index in kept_indices]


class YunetDetector(object):
    backend = "yunet"

    def __init__(self, model_path, score_threshold=0.6, nms_threshold=0.3, top_k=50):
        if not os.path.exists(model_path):
            raise DetectorUnavailable(
                "找不到 YuNet 模型：%s\n"
                "  下载 face_detection_yunet_2023mar.onnx（约 230KB）放到 vision/models/，"
                "或先跑 `scripts\\fetch_face_models.ps1`" % model_path
            )
        self.model_path = model_path
        self.score_threshold = float(score_threshold)
        self.nms_threshold = float(nms_threshold)
        try:
            self._impl = cv2.FaceDetectorYN.create(
                model_path, "", (320, 320),
                self.score_threshold, self.nms_threshold, int(top_k),
            )
        except cv2.error as exc:
            raise DetectorUnavailable(
                "YuNet 加载失败：%s\n"
                "  多半是 OpenCV < 4.5.4，或模型文件下坏了（比对 sha256）" % exc
            )
        self._size = (0, 0)

    def detect(self, frame):
        height, width = frame.shape[:2]
        if self._size != (width, height):
            self._impl.setInputSize((width, height))
            self._size = (width, height)
        try:
            _retval, faces = self._impl.detect(frame)
        except cv2.error:
            return []
        if faces is None:
            return []
        out = []
        for row in np.asarray(faces, dtype=np.float32):
            x, y, w, h = row[0], row[1], row[2], row[3]
            score = float(row[14]) if len(row) >= 15 else 0.0
            marks = None
            if len(row) >= 15:
                marks = [(float(row[4 + 2 * i]), float(row[5 + 2 * i])) for i in range(5)]
            out.append(FaceDetection(x, y, w, h, score, marks, raw_row=row.copy()))
        return out

    def describe(self):
        return "yunet(%s, score>=%.2f)" % (os.path.basename(self.model_path), self.score_threshold)


class HaarDetector(object):
    backend = "haar"

    def __init__(self, cascade_path="", min_face_px=80, min_neighbors=5, scale_factor=1.1,
                 equalize=True):
        path = cascade_path or os.path.join(
            cv2.data.haarcascades, "haarcascade_frontalface_default.xml"
        )
        self.cascade_path = path
        self.min_face_px = int(min_face_px)
        self.min_neighbors = int(min_neighbors)
        self.scale_factor = float(scale_factor)
        # 由 detection.haar_equalize 选择预处理，避免把单一场景的效果外推到所有环境。
        # 默认保持 True，向后兼容旧配置。
        self.equalize = bool(equalize)
        if not os.path.exists(path):
            raise DetectorUnavailable("找不到 Haar 级联文件：%s" % path)
        self._cascade = cv2.CascadeClassifier(path)
        if self._cascade.empty():
            raise DetectorUnavailable("Haar 级联加载失败：%s" % path)

    def detect(self, frame):
        gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
        if self.equalize:
            gray = cv2.equalizeHist(gray)
        boxes = self._cascade.detectMultiScale(
            gray,
            scaleFactor=self.scale_factor,
            minNeighbors=self.min_neighbors,
            minSize=(self.min_face_px, self.min_face_px),
            flags=cv2.CASCADE_SCALE_IMAGE,
        )
        return suppress_nested_detections(
            [FaceDetection(x, y, w, h, 0.99) for (x, y, w, h) in boxes]
        )

    def describe(self):
        return "haar(%s, min=%dpx, equalize=%s, 抑制嵌套小框(面积<=大框50%%且覆盖>=90%%), 无关键点→识别降级)" % (
            os.path.basename(self.cascade_path), self.min_face_px, self.equalize
        )


def create_detector(cfg, log=None):
    """按配置造检测器。`auto` = 有模型用 YuNet，没有就降级 Haar 并**大声说出来**。"""
    log = log or (lambda msg: None)
    det_cfg = cfg["detection"]
    backend = str(det_cfg["backend"])
    model_path = config.resolve_vision(det_cfg["model"])
    has_model = os.path.exists(model_path)

    if backend == "auto":
        backend = "yunet" if has_model else "haar"
        if not has_model:
            log("[vision] 警告：没找到 YuNet 模型，检测降级为 Haar（识别将只有陌生人）")
            log("        补模型：用浏览器下 face_detection_yunet_2023mar.onnx → vision/models/")

    if backend == "yunet":
        if not has_model:
            raise DetectorUnavailable(
                "配置要求 yunet，但模型不存在：%s" % model_path
            )
        return YunetDetector(
            model_path,
            score_threshold=det_cfg["score_threshold"],
            nms_threshold=det_cfg["nms_threshold"],
            top_k=det_cfg["top_k"],
        )
    return HaarDetector(
        cascade_path=det_cfg["haar_cascade"],
        min_face_px=det_cfg["min_face_px"],
        min_neighbors=det_cfg["haar_min_neighbors"],
        equalize=det_cfg.get("haar_equalize", True),
    )
