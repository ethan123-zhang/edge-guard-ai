"""人脸识别：SFace（128 维特征 + 余弦相似度）或空实现。

关键事实（2026-10-23 本机实测）：
- SFace 的 `alignCrop` 必须吃 **YuNet 的 5 点关键点**；Haar 没有关键点 → 只能降级为陌生人。
- SFace 官方建议的余弦阈值是 **0.363**（`docs/02` §7.1），比对人脸时不要凭感觉调。

特征向量**一律 L2 归一化后**再比（点积 = 余弦），
这样阈值在 PC 和板端、不同批次注册之间都可比。
"""
import os

import cv2
import numpy as np

from vision import config


class RecognizerUnavailable(RuntimeError):
    """SFace 模型缺失/加载失败。"""


class NullRecognizer(object):
    """没有 SFace 时的降级：**能检测、不能认人**。链路照跑，事件照发（全部 is_known=false）。"""

    backend = "none"
    available = False

    def embed(self, frame, detection):
        return None

    def describe(self):
        return "none（没有 SFace 模型：所有脸按陌生人上报）"


class SfaceRecognizer(object):
    backend = "sface"
    available = True

    def __init__(self, model_path):
        if not os.path.exists(model_path):
            raise RecognizerUnavailable(
                "找不到 SFace 模型：%s\n"
                "  下载 face_recognition_sface_2021dec.onnx（约 37MB）放到 vision/models/" % model_path
            )
        self.model_path = model_path
        try:
            self._impl = cv2.FaceRecognizerSF.create(model_path, "")
        except cv2.error as exc:
            raise RecognizerUnavailable("SFace 加载失败：%s" % exc)

    def embed(self, frame, detection):
        """返回 L2 归一化后的 128 维向量；无法对齐时返回 None。"""
        raw = getattr(detection, "raw_row", None)
        if raw is None:
            return None
        try:
            aligned = self._impl.alignCrop(frame, np.asarray(raw, dtype=np.float32))
            feat = self._impl.feature(aligned)
        except cv2.error:
            return None
        vec = np.asarray(feat, dtype=np.float32).reshape(-1)
        norm = float(np.linalg.norm(vec))
        if norm <= 1e-6:
            return None
        return vec / norm

    def describe(self):
        return "sface(%s)" % os.path.basename(self.model_path)


def create_recognizer(cfg, log=None):
    log = log or (lambda msg: None)
    rec_cfg = cfg["recognition"]
    backend = str(rec_cfg["backend"])
    model_path = config.resolve_vision(rec_cfg["model"])
    has_model = os.path.exists(model_path)

    if backend == "auto":
        backend = "sface" if has_model else "none"
        if not has_model:
            log("[vision] 警告：没找到 SFace 模型，识别降级为 none（全部当陌生人）")
            log("        补模型：face_recognition_sface_2021dec.onnx → vision/models/")

    if backend == "sface":
        if not has_model:
            raise RecognizerUnavailable("配置要求 sface，但模型不存在：%s" % model_path)
        return SfaceRecognizer(model_path)
    return NullRecognizer()
