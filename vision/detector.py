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

    def __init__(self, cascade_path="", min_face_px=80, min_neighbors=5, scale_factor=1.1):
        path = cascade_path or os.path.join(
            cv2.data.haarcascades, "haarcascade_frontalface_default.xml"
        )
        self.cascade_path = path
        self.min_face_px = int(min_face_px)
        self.min_neighbors = int(min_neighbors)
        self.scale_factor = float(scale_factor)
        if not os.path.exists(path):
            raise DetectorUnavailable("找不到 Haar 级联文件：%s" % path)
        self._cascade = cv2.CascadeClassifier(path)
        if self._cascade.empty():
            raise DetectorUnavailable("Haar 级联加载失败：%s" % path)

    def detect(self, frame):
        gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
        gray = cv2.equalizeHist(gray)
        boxes = self._cascade.detectMultiScale(
            gray,
            scaleFactor=self.scale_factor,
            minNeighbors=self.min_neighbors,
            minSize=(self.min_face_px, self.min_face_px),
            flags=cv2.CASCADE_SCALE_IMAGE,
        )
        return [FaceDetection(x, y, w, h, 0.99) for (x, y, w, h) in boxes]

    def describe(self):
        return "haar(%s, min=%dpx, 无关键点→识别降级)" % (
            os.path.basename(self.cascade_path), self.min_face_px
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
    )
