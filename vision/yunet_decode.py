"""YuNet 先验框解码（先验表预计算版）。

**为什么有这一版**：旧解码器每帧都在纯 Python 里重建 5875 个先验框
（四档网格循环 append tuple），板端实测占掉检测段 31 ms —— TensorRT 把
engine 压到 11.7 ms，解码又吐回去一半。先验表只跟输入尺寸有关、与帧无关，
模块加载时算一次即可，逐帧该做的事情只有「按分数筛 + 解偏移 + NMS」。

**与旧实现的等价性**是这个模块的唯一硬约束：
`logs/video-validation/legacy-pipeline-probe.py` 的 `decode()` 是板端回归的
参照物（`trt_regression.py` 用它比对 CPU/TRT 两条路径），本模块必须逐帧输出
一致（检出脸数、bbox、5 点、score 全部相同，误差只允许来自 float32 舍入）。
因此下面的公式顺序与旧实现一一对应，**不要"顺手优化"成向量化写法**，
除非重新跑 parity 证明逐帧一致。

先验表顺序必须与旧实现相同：`for y -> for x -> for mins`（y 外、x 中、m 内），
因为 ONNX 输出行号与先验序号绑定，换顺序不会报错但会把框全部解错。

用法：

    rows = decode(detector.forward(["loc", "conf", "iou"]))
    for row in rows:
        row[0:4]    # bbox x, y, w, h（输入图上的像素坐标）
        row[4:14]   # 5 个关键点 x, y（右眼、左眼、鼻尖、右嘴角、左嘴角）
        row[14]     # score

板端 Python 3.6：不用 dataclasses / 海象运算符 / `from __future__`。
"""
import cv2
import numpy as np

# 四档先验网格：(网格边长, 步长, 每点人脸尺寸)。顺序与旧解码器完全一致。
SCALES = (
    (40, 8, (10, 16, 24)),
    (20, 16, (32, 48)),
    (10, 32, (64, 96)),
    (5, 64, (128, 192, 256)),
)

# 先验框总数：40²·3 + 20²·2 + 10²·2 + 5²·3 = 4800 + 800 + 200 + 75 = 5875。
PRIOR_COUNT = sum(size * size * len(mins) for size, _step, mins in SCALES)

LOC_COLUMNS = 14  # loc 每个先验 14 列：4 偏移 + 10 个关键点偏移
ROW_COLUMNS = 15  # 解码后 15 列：bbox 4 + 关键点 10 + score 1

SCORE_THRESHOLD = 0.9  # 与旧解码器一致：score >= 0.9 才留
NMS_THRESHOLD = 0.3
NMS_TOP_K = 5000


def _build_priors():
    """按旧实现的顺序（y 外 / x 中 / m 内）生成 (PRIOR_COUNT, 4) 的 float32 先验表。

    每行是 `(cx, cy, w, h)`：`cx = (x + 0.5) * step`，w/h 取该档的人脸尺寸。
    """
    blocks = []
    for size, step, mins in SCALES:
        indexes = np.arange(size * size)
        cx = (indexes % size + 0.5) * step
        cy = (indexes // size + 0.5) * step
        block = np.empty((size * size * len(mins), 4), dtype=np.float32)
        for offset, face_size in enumerate(mins):
            block[offset::len(mins)] = np.column_stack(
                (cx, cy, np.full(size * size, face_size), np.full(size * size, face_size))
            )
        blocks.append(block)
    return np.concatenate(blocks)


# 模块加载时算一次并复用：解码热路径里不再构造单元格子。
_PRIORS = _build_priors()


def priors():
    """返回缓存的先验表（只读用途；解码内部直接引用模块级数组，不拷贝）。"""
    return _PRIORS


def decode(outputs, score_threshold=SCORE_THRESHOLD):
    """把 YuNet 的原始输出解码成 15 列数组列表（bbox + 5 关键点 + score）。

    `outputs` 是 `[loc, conf, overlap]`：
      - `loc`     (PRIOR_COUNT, 14)
      - `conf`    (PRIOR_COUNT, 2)（第 1 列是分类置信度）
      - `overlap` (PRIOR_COUNT,) 或 (PRIOR_COUNT, 1)

    `score_threshold` 默认 0.9（旧解码器的取值，P0 parity 与板端回归靠它）。
    生产侧想沿用 `detection.score_threshold`（默认 0.6）时显式传进来即可——
    注意 NMS 的分数门槛也用同一个值，与 `cv2.FaceDetectorYN` 的语义一致。

    返回按 NMS 保留下来的行，每行 `np.float32[15]`；没人脸时返回 `[]`。
    """
    loc, conf, overlap = outputs
    priors = _PRIORS
    if loc.shape != (priors.shape[0], LOC_COLUMNS):
        raise ValueError(
            "loc 形状不对：%r（期望 %r）" % (loc.shape, (priors.shape[0], LOC_COLUMNS))
        )
    score = np.sqrt(np.maximum(0, conf[:, 1]) * np.clip(overlap.reshape(-1), 0, 1))
    mask = score >= score_threshold
    if not np.any(mask):
        return []
    boxes_xywh = priors[mask]
    offsets = loc[mask]
    score = score[mask]
    # 以下四行照抄旧解码器的公式顺序，parity 依赖它们不能变。
    center = boxes_xywh[:, :2] + offsets[:, :2] * 0.1 * boxes_xywh[:, 2:]
    wh = boxes_xywh[:, 2:] * np.exp(offsets[:, 2:4] * np.array([0.1, 0.2]))
    boxes = np.column_stack((center - wh / 2, wh))
    points = boxes_xywh[:, None, :2] + offsets[:, 4:14].reshape(-1, 5, 2) * 0.1 * boxes_xywh[:, None, 2:]
    rows = np.column_stack((boxes, points.reshape(-1, 10), score)).astype(np.float32)
    keep = cv2.dnn.NMSBoxes(
        boxes.astype(int).tolist(),
        score.tolist(),
        score_threshold,
        NMS_THRESHOLD,
        top_k=NMS_TOP_K,
    )
    return [rows[int(index)] for index in np.asarray(keep).reshape(-1)]
