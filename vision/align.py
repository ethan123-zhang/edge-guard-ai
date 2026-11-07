"""五点对齐：把人脸按 YuNet 的 5 个关键点仿射到 SFace 要的 112×112。

**为什么自己写**：板上 OpenCV 4.1.1 没有 `cv2.FaceRecognizerSF.alignCrop`，
而 SFace 模型本身能吃 `cv2.dnn` 的 ONNX 前向。所以对齐这一步必须自己来——
算法与 `logs/video-validation/legacy-pipeline-probe.py` 的 `align()` 完全一致
（同一份 SVD 求相似变换、同一组目标点、同样的 warpAffine），板端回归用它算出的
特征已经验证过与原生 alignCrop 等价。

目标点是 SFace 官方的标准五人脸模板（左眼、右眼、鼻尖、左嘴角、右嘴角），
顺序必须与 YuNet 输出的 5 个关键点一一对应（右眼、左眼、鼻尖、右嘴角、左嘴角）。
两者顺序不同是官方模型的既有约定；顺序错不会报错，只会得到一张旋转 180° 的废图，
特征照常算得出来但完全没意义 —— 所以顺序由单测钉住，别凭感觉改。

板端 Python 3.6：不用 dataclasses / 海象运算符 / `from __future__`。
"""
import cv2
import numpy as np

# SFace 官方 112×112 模板点（左眼、右眼、鼻尖、左嘴角、右嘴角）
ALIGN_TARGET = np.array([
    [38.2946, 51.6963],
    [73.5318, 51.5014],
    [56.0252, 71.7366],
    [41.5493, 92.3655],
    [70.7299, 92.2041],
], dtype=np.float64)

ALIGN_SIZE = (112, 112)


def similarity_matrix(points):
    """5 个关键点 → 到 `ALIGN_TARGET` 的 2×3 相似变换矩阵（含旋转、缩放、平移）。"""
    source = np.asarray(points, dtype=np.float64).reshape(5, 2)
    source_mean = source.mean(axis=0)
    target_mean = ALIGN_TARGET.mean(axis=0)
    centred_source = source - source_mean
    centred_target = ALIGN_TARGET - target_mean
    u, s, vt = np.linalg.svd(centred_target.T.dot(centred_source) / 5.0)
    signs = np.ones(2)
    if np.linalg.det(u.dot(vt)) < 0:
        signs[-1] = -1.0
    rotation = u.dot(np.diag(signs)).dot(vt)
    scale = s.dot(signs) / np.sum(centred_source * centred_source) * 5.0
    return np.column_stack((scale * rotation, target_mean - scale * rotation.dot(source_mean)))


def crop(image, points, size=ALIGN_SIZE):
    """按关键点裁出对齐后的人脸图；`points` 是 5×2 的人脸坐标。"""
    matrix = similarity_matrix(points)
    return cv2.warpAffine(image, matrix, (int(size[0]), int(size[1])))


def residual(points):
    """对齐残差：拟合后 5 点与模板的 RMS 误差（像素）。质量门里会除以眼距归一化。"""
    matrix = similarity_matrix(points)
    source = np.asarray(points, dtype=np.float64).reshape(5, 2)
    predicted = (matrix[:, :2].dot(source.T) + matrix[:, 2:3]).T
    delta = predicted - ALIGN_TARGET
    return float(np.sqrt(np.mean(np.sum(delta * delta, axis=1))))