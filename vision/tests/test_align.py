"""五点对齐的数学性质 + 与 OpenCV 原生 `alignCrop` 的等价性。

对齐是 SFace 的入口：点序错、缩放错都不会报错，只会让特征变成噪声。
所以这里钉三件事：

1. 相似变换对**平移 / 缩放 / 旋转**都是精确的（残差 ≈ 0）；
2. 关键点顺序（右眼 x < 左眼 x，嘴角 y > 眼睛 y）不能反；
3. 本机有 `FaceRecognizerSF` 时，与官方 `alignCrop` 逐像素比 —— 板上没有这个
   API（OpenCV 4.1.1），所以那条用例在板上 skip，不假装通过。
"""
import os
import shutil
import tempfile
import unittest

import cv2
import numpy as np

from vision import align, config

FAKE_FACE = np.array([
    [80.0, 90.0],    # 右眼（画面左）
    [120.0, 90.0],   # 左眼（画面右）
    [100.0, 115.0],  # 鼻尖
    [85.0, 140.0],   # 右嘴角
    [115.0, 140.0],  # 左嘴角
], dtype=np.float64)


def textured_image(seed=7, size=220):
    """平滑的合成图：随机噪声会在缩放/插值时剧烈变化，不适合做不变性比较。"""
    y, x = np.mgrid[0:size, 0:size].astype(np.float32)
    base = np.stack([x * 0.7 + y * 0.2, y * 0.8 + 20.0, (x + y) * 0.3], axis=-1)
    blobs = np.zeros((size, size), dtype=np.float32)
    for cx, cy, radius in ((70.0, 90.0, 26.0), (140.0, 120.0, 34.0), (95.0, 160.0, 18.0)):
        blobs += 90.0 * np.exp(-(((x - cx) ** 2 + (y - cy) ** 2) / (2.0 * radius ** 2)))
    image = np.clip(base + blobs[:, :, None], 0, 255).astype(np.uint8)
    return cv2.GaussianBlur(image, (5, 5), 1.2)


def similarity_of(points, scale=1.0, angle_deg=0.0, offset=(0.0, 0.0)):
    """对一组点做相似变换（缩放 → 旋转 → 平移），用于"精确拟合"用例。"""
    points = np.asarray(points, dtype=np.float64)
    centre = points.mean(axis=0)
    theta = np.deg2rad(angle_deg)
    rotation = np.array([[np.cos(theta), -np.sin(theta)], [np.sin(theta), np.cos(theta)]])
    moved = (points - centre) * scale
    return moved.dot(rotation.T) + centre + np.asarray(offset, dtype=np.float64)


def face_row(x, y, w, h, points, score=0.95):
    """拼一行 YuNet 15 列输出（bbox + 5 点 + score），对齐只吃 4:14。"""
    row = np.zeros(15, dtype=np.float32)
    row[0:4] = (x, y, w, h)
    row[4:14] = np.asarray(points, dtype=np.float32).reshape(-1)
    row[14] = score
    return row


class TestSimilarityMath(unittest.TestCase):
    def test_target_points_fit_exactly(self):
        self.assertLess(align.residual(align.ALIGN_TARGET), 1e-9)

    def test_translated_points_fit_exactly(self):
        self.assertLess(align.residual(similarity_of(align.ALIGN_TARGET, offset=(37.0, -12.0))), 1e-9)

    def test_scaled_points_fit_exactly(self):
        self.assertLess(align.residual(similarity_of(align.ALIGN_TARGET, scale=2.7)), 1e-9)

    def test_rotated_points_fit_exactly(self):
        self.assertLess(align.residual(similarity_of(align.ALIGN_TARGET, angle_deg=63.0)), 1e-9)

    def test_one_perturbed_point_raises_residual(self):
        points = FAKE_FACE.copy()
        points[2] += np.array([4.0, 4.0])
        self.assertGreater(align.residual(points), 0.5)


class TestLandmarkOrder(unittest.TestCase):
    def test_eyes_are_image_left_to_right(self):
        target = align.ALIGN_TARGET
        self.assertLess(target[0][0], target[1][0], "第一个点必须是画面左侧的那只眼")

    def test_mouth_below_eyes(self):
        target = align.ALIGN_TARGET
        self.assertGreater(min(target[3][1], target[4][1]), max(target[0][1], target[1][1]))


class TestCrop(unittest.TestCase):
    def test_crop_returns_112_square_uint8(self):
        image = textured_image()
        out = align.crop(image, FAKE_FACE)
        self.assertEqual(out.shape, (112, 112, 3))
        self.assertEqual(out.dtype, np.uint8)

    def test_crop_is_translation_invariant(self):
        """同一张脸在画面里挪 6 像素：对齐后应该几乎一样（边界插值留一点余量）。"""
        image = textured_image()
        dx, dy = 6, -4
        shifted = np.roll(image, shift=(dy, dx), axis=(0, 1))
        first = align.crop(image, FAKE_FACE)
        second = align.crop(shifted, FAKE_FACE + np.array([dx, dy]))
        diff = np.abs(first.astype(np.float32) - second.astype(np.float32)).mean()
        self.assertLess(diff, 6.0, "平移 %d,%d 后对齐结果差异 %.2f" % (dx, dy, diff))

    def test_crop_is_scale_invariant(self):
        """整幅放大 1.5 倍、关键点同步放大：对齐结果必须基本不变。

        注意缩放是**关于原点**的（`cv2.resize` 就是这样）：只缩放不平移时，
        相似变换 S(f·p) 恰好等于 S(p)，所以两张对齐图应当重合。
        若改成"关于人脸中心缩放"，就得配一个平移，否则差的是平移量本身。
        """
        image = textured_image()
        factor = 1.5
        scaled_image = cv2.resize(image, None, fx=factor, fy=factor,
                                  interpolation=cv2.INTER_LINEAR)
        first = align.crop(image, FAKE_FACE)
        second = align.crop(scaled_image, FAKE_FACE * factor)
        diff = np.abs(first.astype(np.float32) - second.astype(np.float32)).mean()
        self.assertLess(diff, 8.0, "放大 %.1f 倍后对齐结果差异 %.2f" % (factor, diff))


class TestAgainstOpenCvAlignCrop(unittest.TestCase):
    def setUp(self):
        model = config.resolve_vision("models/face_recognition_sface_2021dec.onnx")
        if not hasattr(cv2, "FaceRecognizerSF"):
            self.skipTest("本机 OpenCV 没有 FaceRecognizerSF（板上就是这样）")
        if not os.path.exists(model):
            self.skipTest("没有 SFace 模型：%s" % model)
        # OpenCV 的 ONNX 导入器按窄字符 fopen 打开文件，仓库路径里的中文读不了
        # （P0 踩过同一个坑），所以拷一份到 ASCII 临时目录再加载。
        self._tmp = tempfile.TemporaryDirectory()
        local = os.path.join(self._tmp.name, "sface.onnx")
        shutil.copy2(model, local)
        self.native = cv2.FaceRecognizerSF.create(local, "")

    def tearDown(self):
        if getattr(self, "_tmp", None) is not None:
            self._tmp.cleanup()

    def test_matches_native_aligncrop(self):
        image = textured_image()
        row = face_row(50, 60, 100, 100, FAKE_FACE)
        mine = align.crop(image, row[4:14].reshape(5, 2))
        native = self.native.alignCrop(image, row)
        diff = float(np.abs(mine.astype(np.float64) - native.astype(np.float64)).mean())
        self.assertLess(diff, 1.0, "与原生 alignCrop 平均像素差 %.3f" % diff)


class TestQuality(unittest.TestCase):
    """关联层的质量门指标：对齐残差 ÷ 眼距（越小越好，<=0.10 才入册）。"""

    def test_perfect_template_scores_zero(self):
        self.assertAlmostEqual(align.quality(align.ALIGN_TARGET), 0.0, places=6)

    def test_is_residual_over_eye_distance(self):
        points = align.ALIGN_TARGET * 80.0 + np.array([200.0, 120.0])
        weird = points.copy()
        weird[4] += np.array([40.0, 0.0])      # 只把嘴角拽歪：不是相似变换
        expected = align.residual(weird) / align.eye_distance(weird)
        self.assertAlmostEqual(align.quality(weird), expected, places=9)
        self.assertGreater(align.quality(weird), align.quality(points))

    def test_eye_distance_uses_first_two_points(self):
        points = align.ALIGN_TARGET.copy()
        self.assertAlmostEqual(align.eye_distance(points),
                               float(np.linalg.norm(points[1] - points[0])), places=9)

    def test_degenerate_eye_distance_is_none_not_huge(self):
        points = align.ALIGN_TARGET.copy()
        points[1] = points[0]                  # 两眼重合 = 坏检测
        self.assertIsNone(align.quality(points))

    def test_matches_the_offline_study_formula(self):
        """2026-11-06 质量研究用的是 rms / max(eye_px, 1e-6)，这里必须逐字一致。"""
        points = FAKE_FACE.copy()
        expected = (align.residual(points) /
                    max(float(np.linalg.norm(points[1] - points[0])), 1e-6))
        self.assertAlmostEqual(align.quality(points), expected, places=12)


if __name__ == "__main__":
    unittest.main()