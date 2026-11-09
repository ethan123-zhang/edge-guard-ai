"""生成标注草稿（`sentinel/scenario-annotation@1`），每 6 帧一标。

两遍走：先采集所有人脸样本并做 SFace 余弦聚类（>=0.5 同人），再只把出现 >=3 次的簇
升成身份字母 A/B/…，其余（边缘残影、抖动、单帧误检）统一标 "?" —— 这样字母不会
被一帧噪声占掉。**草稿不是真值**：谁=A、哪个子例要人工确认。

用法：py -3.12 scripts\make_annotations.py <视频> [<视频> ...]
"""
import json
import os
import sys

import cv2
import numpy as np

from vision import config, detector, recognizer
from check_sc01_identity import ascii_copy

SAMPLE_STEP = 6          # 与评估抽样对齐（协议 §3）
MIN_FACE_PX = 100        # 小于此宽度不建身份（边缘残影/远处背景）
MIN_HITS = 5             # 少于 5 帧的簇不升身份，标 "?"（3 帧的簇基本是走位残影）
SAME_PERSON = 0.50
LETTERS = "ABCDEFGH"
SCENARIOS = {"sc01": "replacement", "sc02": "adjacent",
             "sc03": "recovery", "sc04": "ambiguity"}


def build():
    cfg = config.load("sim")
    cfg["detection"]["model"] = ascii_copy(config.resolve_vision(cfg["detection"]["model"]))
    cfg["recognition"]["model"] = ascii_copy(config.resolve_vision(cfg["recognition"]["model"]))
    return detector.create_detector(cfg), recognizer.create_recognizer(cfg)


def annotate(video, scenario, step=SAMPLE_STEP, keep_letters=None, rename_map=None):
    """keep_letters：只保留这些字母的身份，其余降级成 "?"（排除窗外路人）。
    rename_map：字母改名，如 {"C": "B"}（人工确认谁是谁后统一编号）。"""
    keep_letters = set(keep_letters or [])
    rename_map = dict(rename_map or {})
    det, rec = build()
    cap = cv2.VideoCapture(video)
    fps = float(cap.get(cv2.CAP_PROP_FPS) or 30.0)
    width = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
    height = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
    total = int(cap.get(cv2.CAP_PROP_FRAME_COUNT) or 0)
    scale = width / 640.0

    # ---- 第一遍：采集样本（每 6 帧）
    sampled = []
    for index in range(0, total, step):
        cap.set(cv2.CAP_PROP_POS_FRAMES, index)
        ok, frame = cap.read()
        if not ok:
            break
        small = cv2.resize(frame, (640, int(round(height / scale))))
        faces = det.detect(small)
        t_ms = int(round(index / fps * 1000.0))
        items = []
        for face in sorted(faces, key=lambda item: item.x):
            size_full = int(round(face.size * scale))
            vec = None
            if size_full >= MIN_FACE_PX:
                vec = rec.embed(small, face)
            items.append({
                "bbox": [int(round(face.x * scale)), int(round(face.y * scale)),
                         size_full, int(round(face.h * scale))],
                "vec": vec,
                "x0": int(round(face.x * scale)),
            })
        if items:
            sampled.append({"frame_index": index, "t_ms": t_ms, "items": items})
    cap.release()

    # ---- 聚类（按时间顺序，余弦 >= SAME_PERSON 归同簇）
    clusters = []
    for row in sampled:
        for item in row["items"]:
            vec = item["vec"]
            if vec is None:
                item["cluster"] = None
                continue
            best, best_score = None, -1.0
            for index, cluster in enumerate(clusters):
                score = float(np.dot(cluster["mean"], vec))
                if score > best_score:
                    best, best_score = index, score
            if best is not None and best_score >= SAME_PERSON:
                mean = clusters[best]["mean"] * 0.75 + vec * 0.25
                clusters[best]["mean"] = mean / max(1e-6, float(np.linalg.norm(mean)))
                clusters[best]["hits"] += 1
                clusters[best]["times"].append(row["t_ms"])
                item["cluster"] = best
            else:
                clusters.append({"mean": vec.copy(), "hits": 1,
                                 "times": [row["t_ms"]],
                                 "first_x": item["x0"]})
                item["cluster"] = len(clusters) - 1

    kept = [index for index, cluster in enumerate(clusters) if cluster["hits"] >= MIN_HITS]
    kept.sort(key=lambda index: clusters[index]["times"][0])
    letters = {}
    for position, index in enumerate(kept):
        letters[index] = LETTERS[position]
    if keep_letters:
        for index in list(letters):
            if letters[index] not in keep_letters:
                del letters[index]
    if rename_map:
        for index in letters:
            letters[index] = rename_map.get(letters[index], letters[index])

    rows = []
    unknown = 0
    for row in sampled:
        faces = []
        for item in row["items"]:
            identity = letters.get(item["cluster"], "?")
            if identity == "?":
                unknown += 1
            faces.append({"identity": identity, "bbox": item["bbox"]})
        rows.append({"frame_index": row["frame_index"], "t_ms": row["t_ms"],
                     "faces": faces})

    summary = []
    for position, index in enumerate(kept):
        cluster = clusters[index]
        times = cluster["times"]
        summary.append("%s：%d 帧，%.1f–%.1f s，起始 x≈%d px"
                       % (LETTERS[position], cluster["hits"], min(times) / 1000.0,
                          max(times) / 1000.0, cluster["first_x"]))
    meta_path = os.path.splitext(video)[0] + ".capture.json"
    sha = ""
    if os.path.exists(meta_path):
        with open(meta_path, "r", encoding="utf-8") as handle:
            sha = json.load(handle).get("video_sha256", "")
    doc = {
        "schema": "sentinel/scenario-annotation@1",
        "video": os.path.basename(video),
        "video_sha256": sha,
        "fps": round(fps, 2),
        "width": width,
        "height": height,
        "scenario": scenario,
        "notes": "自动草稿：SFace 聚类 tau=%.2f、每 %d 帧、簇出现 < %d 帧标 '?'；%s"
                 % (SAME_PERSON, step, MIN_HITS, "；".join(summary)),
        "frames": rows,
    }
    out = os.path.splitext(video)[0] + ".json"
    with open(out, "w", encoding="utf-8") as handle:
        json.dump(doc, handle, ensure_ascii=False, indent=1)
    print("\n=== %s（%s）→ %s" % (os.path.basename(video), scenario, os.path.basename(out)))
    print("  标注帧 %d，未知/残影样本 %d" % (len(rows), unknown))
    for line in summary:
        print("  " + line)
    return out


if __name__ == "__main__":
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    keep = None
    rename = {}
    paths = []
    for arg in sys.argv[1:]:
        if arg.startswith("--keep="):
            keep = [ch for ch in arg.split("=", 1)[1].replace(",", "")]
        elif arg.startswith("--map="):
            for pair in arg.split("=", 1)[1].split(","):
                if ":" in pair:
                    old, new = pair.split(":", 1)
                    rename[old] = new
        else:
            paths.append(arg)
    for path in paths:
        name = os.path.basename(path)
        scenario = "unknown"
        for prefix, label in SCENARIOS.items():
            if name.startswith(prefix):
                scenario = label
        annotate(path, scenario, keep_letters=keep, rename_map=rename)