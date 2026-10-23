"""人脸库测试：入库/落盘/重载/阈值边界。纯 numpy，不需要摄像头与模型。"""
import os
import tempfile
import unittest

import numpy as np

from vision import gallery


class TestGallery(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.dir = self._tmp.name
        self.store = gallery.FaceGallery(self.dir).load()

    def tearDown(self):
        self._tmp.cleanup()

    def test_starts_empty(self):
        self.assertEqual(len(self.store), 0)
        self.assertEqual(self.store.best(np.array([1.0, 0.0])), (None, None, 0.0))

    def test_add_then_match(self):
        self.store.add("p_10023", "王阿姨", np.array([1.0, 0.0, 0.0]))
        match = self.store.match(np.array([0.9, 0.1, 0.0]), 0.363)
        self.assertIsNotNone(match)
        self.assertEqual(match.face_id, "p_10023")
        self.assertEqual(match.name, "王阿姨")
        self.assertGreater(match.score, 0.9)

    def test_unrelated_face_is_rejected_by_threshold(self):
        self.store.add("p_1", "张叔", np.array([1.0, 0.0, 0.0]))
        self.assertIsNone(self.store.match(np.array([0.0, 1.0, 0.0]), 0.363))

    def test_vectors_are_l2_normalized_on_disk(self):
        self.store.add("p_2", "李姨", np.array([3.0, 4.0]))
        saved = np.load(os.path.join(self.dir, "p_2.npy"))
        self.assertAlmostEqual(float(np.linalg.norm(saved)), 1.0, places=5)

    def test_survives_reload(self):
        self.store.add("p_3", "赵伯", np.array([0.0, 1.0]))
        reloaded = gallery.FaceGallery(self.dir).load()
        self.assertEqual(len(reloaded), 1)
        self.assertEqual(reloaded.names()["p_3"], "赵伯")

    def test_remove_deletes_both_files_and_registry(self):
        self.store.add("p_4", "钱姨", np.array([1.0, 0.0]))
        self.assertTrue(self.store.remove("p_4"))
        self.assertFalse(os.path.exists(os.path.join(self.dir, "p_4.npy")))
        self.assertEqual(gallery.FaceGallery(self.dir).load().names(), {})

    def test_zero_vector_is_rejected(self):
        with self.assertRaises(gallery.GalleryError):
            self.store.add("p_bad", "空向量", np.zeros(4))

    def test_missing_feature_file_is_skipped_not_fatal(self):
        self.store.add("p_5", "孙姨", np.array([1.0, 0.0]))
        os.remove(os.path.join(self.dir, "p_5.npy"))
        reloaded = gallery.FaceGallery(self.dir).load()
        self.assertEqual(len(reloaded), 0)


if __name__ == "__main__":
    unittest.main()
