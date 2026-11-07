"""`vision.yunet_decode` 与旧解码器的逐帧等价性 + 先验表缓存。

旧解码器是板端回归的参照物（`logs/video-validation/legacy-pipeline-probe.py`
的 `decode()`），P0 的全部风险都集中在"预计算之后解出来的框变了没有"。
所以这里把旧实现原样抄一份当 oracle，用合成输出逐帧比对到 float32 位级：

- 先验表顺序（y 外 / x 中 / m 内）必须一致 —— 换顺序不报错但框全错；
- 解码公式顺序不能动，否则 float32 舍入会漂；
- 空结果、NMS 抑制、loc 形状校验、`overlap` 列向量都要钉住。

合成输出而不是真视频：PC 没装 OpenCV 的机器也能跑，真视频的 40 帧 parity
在板端 `trt_regression.py` 上跑（那是验收项，不是单测）。
"""
import os
import sys
import time
import unittest

import cv2
import numpy as np

PROJECT_DIR = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if PROJECT_DIR not in sys.path:
    sys.path.insert(0, PROJECT_DIR)

from vision import yunet_decode

# 旧解码器的四档网格，原样抄（不要改，这是 oracle 的一部分）
LEGACY_SCALES = ((40, 8, (10, 16, 24)), (20, 16, (32, 48)), (10, 32, (64, 96)), (5, 64, (128, 192, 256)))


def legacy_priors():
    priors = []
    for size, step, mins in LEGACY_SCALES:
        for y in range(size):
            for x in range(size):
                for m in mins:
                    priors.append(((x + 0.5) * step, (y + 0.5) * step, m, m))
    return np.array(priors, dtype=np.float32)


def legacy_decode(loc, conf, overlap):
    """旧 `decode()` 的单帧版本：逐行照抄，只把 outputs 拆成三个参数。"""
    priors = legacy_priors()
    assert loc.shape == (len(priors), 14)
    p = priors
    score = np.sqrt(np.maximum(0, conf[:, 1]) * np.clip(overlap.reshape(-1), 0, 1))
    mask = score >= 0.9
    p = p[mask]
    l = loc[mask]
    score = score[mask]
    if not len(p):
        return []
    center = p[:, :2] + l[:, :2] * 0.1 * p[:, 2:]
    wh = p[:, 2:] * np.exp(l[:, 2:4] * np.array([0.1, 0.2]))
    boxes = np.column_stack((center - wh / 2, wh))
    points = p[:, None, :2] + l[:, 4:14].reshape(-1, 5, 2) * 0.1 * p[:, None, 2:]
    rows = np.column_stack((boxes, points.reshape(-1, 10), score)).astype(np.float32)
    keep = cv2.dnn.NMSBoxes(boxes.astype(int).tolist(), score.tolist(), 0.9, 0.3, top_k=5000)
    return [rows[int(i)] for i in np.asarray(keep).reshape(-1)]


def synthetic_outputs(seed=20261107):
    """造一份"只有几处过线、其余全低于阈值"的 YuNet 输出。

    返回 `(loc, conf, overlap, hit_indexes)`；hit 是确定的先验序号，便于断言。
    """
    rng = np.random.RandomState(seed)
    count = yunet_decode.PRIOR_COUNT
    loc = rng.normal(0.0, 0.2, size=(count, 14)).astype(np.float32)
    conf = np.zeros((count, 2), dtype=np.float32)
    conf[:, 1] = rng.uniform(0.0, 0.5, size=count)
    overlap = rng.uniform(0.0, 1.0, size=count)
    # 三个确定的命中点：跨三档网格，覆盖普通框 / 贴边 / 最后一档
    hits = [0, 4801, 5874]
    for index in hits:
        conf[index, 1] = 1.0
        overlap[index] = 1.0
    return loc, conf, overlap, hits


class TestPriors(unittest.TestCase):
    def test_prior_count_and_dtype(self):
        priors = yunet_decode.priors()
        self.assertEqual(priors.shape, (5875, 4))
        self.assertEqual(priors.dtype, np.float32)
        self.assertEqual(yunet_decode.PRIOR_COUNT, 5875)

    def test_prior_order_matches_legacy_loop(self):
        """顺序必须逐行相同：ONNX 输出行号与先验序号绑定，换序会把框全解错。"""
        self.assertTrue(np.array_equal(yunet_decode.priors(), legacy_priors()))

    def test_priors_are_cached(self):
        """同一对象复用：证明没有在热路径里重建格子。"""
        self.assertIs(yunet_decode.priors(), yunet_decode.priors())
        self.assertIs(yunet_decode.priors(), yunet_decode._PRIORS)

    def test_decode_does_not_mutate_cached_priors(self):
        """缓存表被所有帧共享，解码内部若原地改写会污染后续每一帧。"""
        before = yunet_decode.priors().copy()
        loc, conf, overlap, _hits = synthetic_outputs()
        yunet_decode.decode([loc, conf, overlap])
        self.assertTrue(np.array_equal(yunet_decode.priors(), before))


class TestDecodeParity(unittest.TestCase):
    def test_matches_legacy_on_synthetic_outputs(self):
        loc, conf, overlap, hits = synthetic_outputs()
        expected = legacy_decode(loc, conf, overlap)
        actual = yunet_decode.decode([loc, conf, overlap])

        self.assertEqual(len(actual), len(expected))
        self.assertGreaterEqual(len(actual), 1, "构造的命中点应该至少解出一个框")
        for got, want in zip(actual, expected):
            self.assertEqual(got.shape, (15,))
            self.assertEqual(got.dtype, np.float32)
            self.assertTrue(np.array_equal(got, want), "逐位不一致：\n%s\n%s" % (got, want))

    def test_hits_land_on_expected_priors(self):
        """命中点解出来的 bbox 中心必须落在对应先验格子上（钉住映射方向）。"""
        loc, conf, overlap, hits = synthetic_outputs()
        rows = yunet_decode.decode([loc, conf, overlap])
        self.assertEqual(len(rows), len(hits))
        priors = yunet_decode.priors()
        for index, row in zip(hits, rows):
            want_cx = priors[index, 0] + loc[index, 0] * 0.1 * priors[index, 2]
            want_cy = priors[index, 1] + loc[index, 1] * 0.1 * priors[index, 3]
            self.assertAlmostEqual(float(row[0] + row[2] / 2.0), float(want_cx), places=3)
            self.assertAlmostEqual(float(row[1] + row[3] / 2.0), float(want_cy), places=3)

    def test_overlap_as_column_vector_is_equivalent(self):
        """旧探针里 overlap 用 `reshape(-1)` 兼容两种形状，这里同样不能挑形状。"""
        loc, conf, overlap, _hits = synthetic_outputs()
        flat = yunet_decode.decode([loc, conf, overlap])
        column = yunet_decode.decode([loc, conf, overlap.reshape(-1, 1)])
        self.assertEqual(len(flat), len(column))
        for got, want in zip(flat, column):
            self.assertTrue(np.array_equal(got, want))

    def test_empty_when_nothing_passes_threshold(self):
        loc, conf, overlap, _hits = synthetic_outputs()
        conf = conf.copy()
        conf[:, 1] = 0.0
        self.assertEqual(yunet_decode.decode([loc, conf, overlap]), [])

    def test_rejects_wrong_loc_shape(self):
        loc, conf, overlap, _hits = synthetic_outputs()
        with self.assertRaises(ValueError):
            yunet_decode.decode([loc[:-1], conf, overlap])
        with self.assertRaises(ValueError):
            yunet_decode.decode([np.column_stack((loc, loc[:, :1])), conf, overlap])

    def test_nms_actually_suppresses_overlapping_boxes(self):
        """相邻格子、同尺寸、同分数：IoU≈0.33>0.3，NMS 必须只留一个。"""
        count = yunet_decode.PRIOR_COUNT
        loc = np.zeros((count, 14), dtype=np.float32)
        conf = np.zeros((count, 2), dtype=np.float32)
        overlap = np.zeros(count, dtype=np.float32)
        # 尺寸 16 档在 40 格子里的位置：序号 = (y*40 + x)*3 + 1
        first, second = 1, 4  # (y=0,x=0,m=16) 与 (y=0,x=1,m=16)
        for index in (first, second):
            conf[index, 1] = 1.0
            overlap[index] = 1.0
        rows = yunet_decode.decode([loc, conf, overlap])
        self.assertEqual(len(rows), 1)

    def test_decode_is_fast_enough_to_keep_the_gain(self):
        """回归闸门：重建先验的旧实现约 31 ms，这里留 5 倍余量（板端目标 <5 ms）。"""
        loc, conf, overlap, _hits = synthetic_outputs()
        samples = []
        for _ in range(15):
            start = time.perf_counter()
            yunet_decode.decode([loc, conf, overlap])
            samples.append((time.perf_counter() - start) * 1000.0)
        median_ms = sorted(samples)[len(samples) // 2]
        self.assertLess(median_ms, 25.0, "解码中位 %.2f ms，明显回退" % median_ms)


if __name__ == "__main__":
    unittest.main()
