"""素材评估入口（`scripts/scenario_eval.py`）的纯函数部分。

真实的回放跑真实素材（`py -3.12 scripts/scenario_eval.py --video ...`），单测这里只钉
"断言算得对不对"：标注框匹配、4 个场景的判定、混淆矩阵。协议第 4 节明确要求先用合成
标注把入口测通，再拿真实素材复评阈值。
"""
import importlib.util
import json
import os
import tempfile
import unittest

PROJECT_DIR = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
SCRIPT = os.path.join(PROJECT_DIR, "scripts", "scenario_eval.py")


def load_script():
    spec = importlib.util.spec_from_file_location("scenario_eval_under_test", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def report(identity, reported=True, session=1, reason="associated", t_ms=0):
    return {"identity": identity, "reported": reported, "session": session,
            "reason": reason, "t_ms": t_ms}


class GeometryTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.eval = load_script()

    def test_iou_identical_is_one(self):
        self.assertAlmostEqual(self.eval.iou([0, 0, 10, 10], [0, 0, 10, 10]), 1.0)

    def test_iou_disjoint_is_zero(self):
        self.assertEqual(self.eval.iou([0, 0, 10, 10], [100, 100, 10, 10]), 0.0)

    def test_identity_for_bbox_matches_overlap(self):
        faces = [{"identity": "A", "bbox": [100, 100, 80, 80]},
                 {"identity": "B", "bbox": [400, 100, 80, 80]}]
        self.assertEqual(self.eval.identity_for_bbox([104, 102, 78, 80], faces), "A")
        self.assertEqual(self.eval.identity_for_bbox([402, 101, 79, 79], faces), "B")

    def test_identity_for_bbox_returns_unknown(self):
        faces = [{"identity": "A", "bbox": [100, 100, 80, 80]}]
        self.assertEqual(self.eval.identity_for_bbox([900, 900, 50, 50], faces), "?")

    def test_nearest_annotation_walks_back(self):
        frames = [{"frame_index": 0, "faces": [{"identity": "A", "bbox": [1, 1, 9, 9]}]},
                  {"frame_index": 6, "faces": [{"identity": "B", "bbox": [1, 1, 9, 9]}]},
                  {"frame_index": 12, "faces": [{"identity": "A", "bbox": [1, 1, 9, 9]}]}]
        self.assertEqual(self.eval.nearest_annotation(frames, 7)[0]["identity"], "B")
        self.assertEqual(self.eval.nearest_annotation(frames, 99)[0]["identity"], "A")
        self.assertEqual(self.eval.nearest_annotation([], 5), [])

    def test_locate_identity_uses_neighbour_annotations(self):
        # 第 6 帧那一行人是"?"，但前后两帧框基本重合、身份清楚 → 应该认成 B
        frames = [{"frame_index": 0, "faces": [{"identity": "A", "bbox": [100, 100, 80, 80]}]},
                  {"frame_index": 6, "faces": [{"identity": "?", "bbox": [110, 100, 80, 80]}]},
                  {"frame_index": 12, "faces": [{"identity": "B", "bbox": [105, 100, 80, 80]}]}]
        self.assertEqual(self.eval.locate_identity(frames, 7, [108, 100, 80, 80]), "B")
        self.assertEqual(self.eval.locate_identity(frames, 7, [900, 900, 40, 40]), "?")


class SummarizeTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.eval = load_script()

    def test_replacement_clean_pass(self):
        stats = self.eval.summarize("replacement",
                                    [report("A", session=1), report("B", session=2)],
                                    {"A", "B"})
        self.assertTrue(stats["passed"], stats["problems"])
        self.assertEqual(stats["events_by_identity"], {"A": 1, "B": 1})

    def test_replacement_reports_b_inheriting_a_session(self):
        # B 被并进 A 的会话 → B 那次不上报，只剩 A 一条事件
        stats = self.eval.summarize("replacement",
                                    [report("A", session=1),
                                     report("A", reported=False, session=1)],
                                    {"A", "B"})
        self.assertFalse(stats["passed"])
        self.assertTrue(any("身份 B" in text for text in stats["problems"]
                            for _ in [0]))
        self.assertTrue(any("会话" in text for text in stats["problems"]))

    def test_adjacent_needs_two_sessions(self):
        stats = self.eval.summarize("adjacent",
                                    [report("A", session=1), report("B", session=1)],
                                    {"A", "B"})
        self.assertFalse(stats["passed"])
        self.assertTrue(any("会话" in text for text in stats["problems"]))

    def test_recovery_pass_requires_real_suppression(self):
        stats = self.eval.summarize("recovery",
                                    [report("A", session=1),
                                     report("A", reported=False, session=1)],
                                    {"A"})
        self.assertTrue(stats["passed"], stats["problems"])

    def test_recovery_without_suppression_is_unverified(self):
        # 间隙没超过 tracker 10 s 冷却：只上报 1 次，但没有任何"被关联层压掉"的记录
        stats = self.eval.summarize("recovery", [report("A", session=1)], {"A"})
        self.assertFalse(stats["passed"])
        self.assertTrue(any("冷却" in text for text in stats["problems"]))

    def test_recovery_reports_twice_when_association_misses(self):
        stats = self.eval.summarize("recovery",
                                    [report("A", session=1), report("A", session=2)],
                                    {"A"})
        self.assertFalse(stats["passed"])
        self.assertTrue(any("会话 2 条" in text for text in stats["problems"]))

    def test_ambiguity_forced_merge_fails(self):
        stats = self.eval.summarize("ambiguity",
                                    [report("A", session=1), report("B", session=1)],
                                    {"A", "B"})
        self.assertFalse(stats["passed"])
        self.assertTrue(any("强行合并" in text for text in stats["problems"]))

    def test_ambiguity_independent_candidates_pass(self):
        stats = self.eval.summarize("ambiguity",
                                    [report("A", session=1, reported=False),
                                     report("A", session=2),
                                     report("B", session=3)],
                                    {"A", "B"})
        self.assertTrue(stats["passed"], stats["problems"])
        self.assertEqual(stats["events_by_identity"], {"A": 1, "B": 1})


class CosineTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.eval = load_script()

    def test_matrix_separates_same_and_different(self):
        decisions = [
            {"reason": "associated", "own": 0.72, "other": 0.30,
             "session": 1, "identity": "A"},
            {"reason": "associated", "own": 0.55, "other": 0.20,
             "session": 2, "identity": "B"},
            {"reason": "below_tau", "own": 0.40, "other": None,
             "session": 3, "identity": "A"},
            {"reason": "ambiguous_margin", "own": 0.65, "other": 0.60,
             "session": 4, "identity": "B"},
            {"reason": "track_bound", "own": None, "other": None,
             "session": 1, "identity": "A"},
        ]
        sessions = {1: "A", 2: "A", 3: "A", 4: "A"}
        stats = self.eval.cosine_stats(decisions, sessions)
        self.assertEqual(stats["matrix"]["reuse_same"], 1)
        self.assertEqual(stats["matrix"]["reuse_diff"], 1)
        self.assertEqual(stats["matrix"]["reject_diff"], 1)
        self.assertEqual(stats["matrix"]["reject_same"], 1)
        self.assertEqual(stats["same_person_own"]["n"], 2)
        self.assertEqual(stats["different_person_own"]["n"], 2)


class AnnotationLoadingTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.eval = load_script()

    def test_sha_mismatch_is_reported(self):
        with tempfile.TemporaryDirectory() as tmp:
            video = os.path.join(tmp, "clip.mp4")
            with open(video, "wb") as handle:
                handle.write(b"not-really-a-video")
            note = os.path.join(tmp, "clip.json")
            with open(note, "w", encoding="utf-8") as handle:
                json.dump({"schema": "sentinel/scenario-annotation@1",
                           "video": "clip.mp4",
                           "video_sha256": "0" * 64,
                           "fps": 30, "width": 1920, "height": 1080,
                           "scenario": "recovery", "frames": []}, handle)
            doc, problems = self.eval.load_annotation(video)
            self.assertEqual(doc["scenario"], "recovery")
            self.assertTrue(any("sha256" in text for text in problems))

    def test_bad_schema_is_reported(self):
        with tempfile.TemporaryDirectory() as tmp:
            video = os.path.join(tmp, "clip.mp4")
            with open(video, "wb") as handle:
                handle.write(b"x")
            note = os.path.join(tmp, "clip.json")
            with open(note, "w", encoding="utf-8") as handle:
                json.dump({"schema": "something-else", "frames": []}, handle)
            _doc, problems = self.eval.load_annotation(video)
            self.assertTrue(any("schema" in text for text in problems))


if __name__ == "__main__":
    unittest.main()