"""按标注真值回放真实门口素材，核对 4 个场景的断言，并给出 tau/margin 的可选区间。

**为什么要这个入口**：`docs/独立正负例素材采集与标注协议-2026-11-06.md` 第 4 节要求
"在独立素材上重新选 tau/delta"，但原来的评估只在 LFW 与合成夹具上做过。这里把真实素材
喂进**生产同一条链路**（检测 → 五点对齐 → SFace → 跟踪 → 匿名关联 → 上报），用人工
标注当唯一真值，统计"每个身份报了几次、几段会话、有没有把两个人并进同一会话"，并把
关联决策里的 `own`（与本会话相似度）和 `own-other`（与次像会话的差）分布拉出来。

用法（PC 上先跑一条短的看通路）：
    py -3.12 scripts/scenario_eval.py --video datasets/scenarios/real-20261108/sc03_recovery_t1.mp4
换阈值再跑一遍：
    py -3.12 scripts/scenario_eval.py --video ... --tau 0.50 --margin 0.10 --json logs/eval.json
关掉关联层看基线（应该重报）：
    py -3.12 scripts/scenario_eval.py --video ... --no-association

断言（协议第 2 节）：
- replacement：A 报 1 次、B 报 1 次、会话 2 条（B 不得继承 A 的会话）
- adjacent：A、B 各报 1 次、会话 2 条
- recovery：A 只报 1 次、会话 1 条；且"离开再回来"那一次上报必须是真的发生过、被关联层
  按证据压下去的（否则只是 tracker 的 10 s 冷却挡的，这条素材就验证不了关联层）
- ambiguity：不得强行合并（一条会话里不得出现两个身份），会话数 >= 标注身份数

Python 3.6 语法（板端 `vision/tests/test_py36_compat.py` 会做 AST 检查）。
"""
import argparse
import hashlib
import json
import os
import shutil
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

import cv2

from vision import config, gallery, onnx_pipeline
from vision.service import VisionService
from vision.tracker import FaceTracker

BASE_TS = 1700000000000      # 让时间戳像生产环境（毫秒级 epoch）
MATCH_IOU = 0.3              # 标注框与检测框的对应门限
REUSE_REASONS = ("associated",)
REJECT_REASONS = ("new_session", "below_tau", "ambiguous_margin", "session_occupied")


# ---------------------------------------------------------------- 纯函数（可单测）

def iou(box_a, box_b):
    ax, ay, aw, ah = [float(v) for v in box_a]
    bx, by, bw, bh = [float(v) for v in box_b]
    inter_w = max(0.0, min(ax + aw, bx + bw) - max(ax, bx))
    inter_h = max(0.0, min(ay + ah, by + bh) - max(ay, by))
    inter = inter_w * inter_h
    if inter <= 0:
        return 0.0
    union = aw * ah + bw * bh - inter
    return inter / union if union > 0 else 0.0


def identity_for_bbox(bbox, faces, min_iou=MATCH_IOU):
    """在标注帧的 faces 里找和 `bbox` 最像的一张脸，返回其 identity（找不到 = "?"）。"""
    best, best_score = None, 0.0
    for face in faces:
        score = iou(bbox, face["bbox"])
        if score > best_score:
            best, best_score = face, score
    if best is None or best_score < min_iou:
        return "?"
    return best["identity"]


def nearest_annotation(doc_frames, frame_index):
    """最近的标注帧（标注每 6 帧一条，往回找），返回 faces 列表。"""
    if not doc_frames:
        return []
    chosen = doc_frames[0]
    for row in doc_frames:
        if row["frame_index"] <= frame_index:
            chosen = row
        else:
            break
    return chosen["faces"]


def locate_identity(doc_frames, frame_index, bbox, steps=3, min_iou=0.2):
    """在标注帧 ±`steps` 条的窗口里给检测框找身份。

    走位/转身的那几帧，人工标注会因为"身份看不清"标成 "?"，但前后几帧能看清——
    用窗口取 IoU 最高且身份已知的那张脸，避免把真人的上报记成"未知"。
    """
    if not doc_frames:
        return "?"
    current = -1
    for position, row in enumerate(doc_frames):
        if row["frame_index"] <= frame_index:
            current = position
        else:
            break
    best, best_score = None, 0.0
    for position in range(max(0, current - steps), min(len(doc_frames), current + steps + 1)):
        for face in doc_frames[position]["faces"]:
            if face["identity"] == "?":
                continue
            score = iou(bbox, face["bbox"])
            if score > best_score:
                best, best_score = face["identity"], score
    if best is None or best_score < min_iou:
        return "?"
    return best


def _reason_counts(decisions):
    counts = {}
    for row in decisions:
        reason = row.get("reason") or "unknown"
        counts[reason] = counts.get(reason, 0) + 1
    return counts


def summarize(scenario, reports, annotated_identities):
    """统计 + 断言。`reports` 是 tracker 每次"想上报"的记录（含被关联层压掉的）。"""
    emitted = [row for row in reports if row["reported"]]
    by_identity = {}
    for row in emitted:
        if row["identity"] == "?":
            continue
        by_identity[row["identity"]] = by_identity.get(row["identity"], 0) + 1
    sessions = set(row["session"] for row in emitted if row["session"] is not None)
    session_identities = {}
    for row in emitted:
        if row["session"] is None:
            continue
        session_identities.setdefault(row["session"], set()).add(row["identity"])
    merged = sorted(sid for sid, ids in session_identities.items()
                    if len([i for i in ids if i != "?"]) > 1)
    suppressed = [row for row in reports if not row["reported"]]
    stats = {
        "scenario": scenario,
        "report_attempts": len(reports),
        "events": len(emitted),
        "suppressed": len(suppressed),
        "events_by_identity": by_identity,
        "sessions": len(sessions),
        "session_identities": dict((str(k), sorted(v)) for k, v in session_identities.items()),
        "unknown_events": sum(1 for row in emitted if row["identity"] == "?"),
        "merged_sessions": merged,
        "annotated_identities": sorted(annotated_identities),
        "report_reasons": _reason_counts(reports),
    }
    problems = []
    if scenario == "replacement":
        for identity in ("A", "B"):
            if by_identity.get(identity, 0) != 1:
                problems.append("身份 %s 报了 %d 次（应为 1 次）"
                                % (identity, by_identity.get(identity, 0)))
        if len(sessions) != 2:
            problems.append("会话 %d 条（应为 2 条：B 不得继承 A 的会话）" % len(sessions))
    elif scenario == "adjacent":
        for identity in ("A", "B"):
            if by_identity.get(identity, 0) != 1:
                problems.append("身份 %s 报了 %d 次（应为 1 次）"
                                % (identity, by_identity.get(identity, 0)))
        if len(sessions) != 2:
            problems.append("会话 %d 条（应为 2 条）" % len(sessions))
    elif scenario == "recovery":
        if len(emitted) != 1 or by_identity.get("A", 0) != 1:
            problems.append("A 上报了 %d 次（离开再回来应并回原会话、只报 1 次）"
                            % len(emitted))
        if len(sessions) != 1:
            problems.append("会话 %d 条（应为 1 条）" % len(sessions))
        if len(suppressed) < 1:
            problems.append("没有任何被关联层压掉的上报：间隙没超过 tracker 10 s 冷却，"
                            "这条素材验证不了『原人恢复』")
    elif scenario == "ambiguity":
        if merged:
            problems.append("会话 %s 里混进了两个身份（强行合并）" % merged)
        if len(sessions) < len(annotated_identities):
            problems.append("会话 %d 条 < 身份 %d 个（有身份被吞掉）"
                            % (len(sessions), len(annotated_identities)))
    stats["problems"] = problems
    stats["passed"] = not problems
    return stats


def cosine_stats(decisions, session_identities):
    """按"同人 / 异人"拆相似度，并给出关联决策的混淆矩阵。

    - 同人复用（associated & 同一身份）：own 越高越好，低于 tau 就是误拒
    - 异人被拒（below_tau/ambiguous/... & 不同身份）：own 越低越好，高于 tau 就是误并
    """
    same_own, other_own, margins = [], [], []
    matrix = {"reuse_same": 0, "reuse_diff": 0, "reject_same": 0,
              "reject_diff": 0, "unknown": 0}
    for row in decisions:
        reason = row.get("reason")
        if reason == "track_bound":
            continue
        identity = row.get("identity") or "?"
        session_identity = session_identities.get(row.get("session"))
        known = identity != "?" and session_identity not in (None, "?")
        own = row.get("own")
        if own is not None and known:
            if reason in REUSE_REASONS:
                if identity == session_identity:
                    same_own.append(own)
                    matrix["reuse_same"] += 1
                else:
                    other_own.append(own)
                    matrix["reuse_diff"] += 1
            elif reason in REJECT_REASONS:
                if identity == session_identity:
                    same_own.append(own)
                    matrix["reject_same"] += 1
                else:
                    other_own.append(own)
                    matrix["reject_diff"] += 1
            else:
                matrix["unknown"] += 1
        else:
            matrix["unknown"] += 1
        if own is not None and row.get("other") is not None:
            margins.append(own - row["other"])

    def _pack(values):
        if not values:
            return None
        values = sorted(values)
        return {"n": len(values), "min": round(values[0], 3),
                "median": round(values[len(values) // 2], 3),
                "max": round(values[-1], 3)}

    return {"matrix": matrix,
            "same_person_own": _pack(same_own),
            "different_person_own": _pack(other_own),
            "own_minus_other": _pack(margins)}


# ---------------------------------------------------------------- 回放

class RecordingSink(object):
    name = "eval"

    def __init__(self):
        self.messages = []

    def send(self, msg):
        self.messages.append(msg)
        return True

    def close(self):
        pass

    def of_type(self, kind):
        return [m for m in self.messages if m.get("type") == kind]


def _ascii_model_copy(path):
    """本机 cv2/onnx 读不了带中文的路径（项目目录叫「哨兵管家」），拷到临时目录。"""
    path = str(path)
    if not os.path.exists(path):
        return path
    if all(ord(ch) < 128 for ch in path):
        return path
    target_dir = os.path.join(tempfile.gettempdir(), "sentinel-models")
    os.makedirs(target_dir, exist_ok=True)
    target = os.path.join(target_dir, os.path.basename(path))
    if not os.path.exists(target) or os.path.getsize(target) != os.path.getsize(path):
        shutil.copyfile(path, target)
    return target


def _sha256(path):
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def load_annotation(video_path, annotation_path=None):
    path = annotation_path or (os.path.splitext(video_path)[0] + ".json")
    with open(path, "r", encoding="utf-8") as handle:
        doc = json.load(handle)
    problems = []
    if doc.get("schema") != "sentinel/scenario-annotation@1":
        problems.append("schema 不是 scenario-annotation@1：%r" % doc.get("schema"))
    expected = doc.get("video_sha256")
    if expected and _sha256(video_path) != expected:
        problems.append("视频 sha256 与标注不一致（素材被改过/剪辑过）")
    return doc, problems


def build_service(cfg, log):
    detector, recognizer = onnx_pipeline.create_pipeline(cfg, log=log)
    tracker = FaceTracker(
        confirm_frames=int(cfg["recognition"]["confirm_frames"]),
        cooldown_sec=float(cfg["recognition"]["cooldown_sec"]),
        track_ttl_sec=float(cfg["recognition"]["track_ttl_sec"]),
    )
    store = gallery.FaceGallery(os.path.join(tempfile.gettempdir(),
                                             "sentinel-eval-gallery")).load()
    sink = RecordingSink()
    service = VisionService(cfg, None, detector, recognizer, store, tracker, sink,
                            log=log)
    return service, sink


def replay(video_path, doc, cfg, stride, log):
    service, sink = build_service(cfg, log)
    cap = cv2.VideoCapture(video_path)
    if not cap.isOpened():
        raise RuntimeError("打不开视频：%s" % video_path)
    fps = float(doc.get("fps") or cap.get(cv2.CAP_PROP_FPS) or 30.0)
    width = int(doc.get("width") or cap.get(cv2.CAP_PROP_FRAME_WIDTH))
    height = int(doc.get("height") or cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
    frames = doc.get("frames") or []
    reports = []
    decisions = []
    current = {"frame_index": 0, "identity": "?"}

    original_report = service._should_report
    original_observe = service.associator.observe

    def traced_observe(track_id, embedding, rms_norm, frame_ts, tracked_ids=None):
        result = original_observe(track_id, embedding, rms_norm, frame_ts,
                                 tracked_ids=tracked_ids)
        box = None
        for track in getattr(service.tracker, "_tracks", []):
            if track.id == int(track_id):
                box = track.bbox
                break
        identity = "?"
        if box is not None:
            identity = locate_identity(frames, current["frame_index"], box)
        decisions.append({
            "t_ms": int(frame_ts) - BASE_TS,
            "frame_index": current["frame_index"],
            "track_id": int(track_id),
            "identity": identity,
            "reason": result.reason,
            "own": None if result.own_best is None else round(result.own_best, 3),
            "other": None if result.other_best is None else round(result.other_best, 3),
            "session": result.session_id,
            "rms_norm": None if rms_norm is None else round(float(rms_norm), 4),
        })
        return result

    def traced_report(event, results):
        decision = original_report(event, results)
        result = results.get(event.track_id)
        identity = locate_identity(frames, current["frame_index"], event.bbox)
        reports.append({
            "t_ms": int(event.frame_ts) - BASE_TS,
            "frame_index": current["frame_index"],
            "track_id": int(event.track_id),
            "bbox": [int(v) for v in event.bbox],
            "identity": identity,
            "reported": bool(decision),
            "reason": None if result is None else result.reason,
            "own": None if result is None or result.own_best is None
            else round(result.own_best, 3),
            "session": None if result is None else result.session_id,
        })
        return decision

    service._should_report = traced_report
    service.associator.observe = traced_observe
    service._blank_std = 0.0        # 素材里本来就有人走动，别让空白帧保护吞帧

    processed = 0
    frame_index = 0
    while True:
        ok, frame = cap.read()
        if not ok or frame is None:
            break
        current["frame_index"] = frame_index
        if frame.shape[1] != width or frame.shape[0] != height:
            frame = cv2.resize(frame, (width, height))
        if frame_index % stride == 0:
            ts = BASE_TS + int(round(frame_index / fps * 1000.0))
            service._process_frame(frame, ts)
            processed += 1
        frame_index += 1
    cap.release()

    session_identities = {}
    for row in reports:
        if row["session"] is None or row["identity"] == "?":
            continue
        session_identities[row["session"]] = row["identity"]
    # "associated" 说明这条轨道和该会话是同一人，可以补上更早的身份映射
    for row in decisions:
        if row["reason"] in REUSE_REASONS and row["session"] is not None \
                and row["session"] not in session_identities \
                and row["identity"] != "?":
            session_identities[row["session"]] = row["identity"]
    return (reports, decisions, session_identities, processed, frame_index, sink)


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="按标注真值回放素材，核对关联层断言（4 个场景）")
    parser.add_argument("--video", required=True, help="素材路径（同名 .json 是标注）")
    parser.add_argument("--annotations", default=None, help="标注 JSON（默认同名）")
    parser.add_argument("--profile", default="sim", help="配置档（默认 sim）")
    parser.add_argument("--backend", default="cpu", help="runtime.backend（默认 cpu）")
    parser.add_argument("--stride", type=int, default=3,
                        help="每几帧处理一次（默认 3；越小越接近生产时序）")
    parser.add_argument("--tau", type=float, default=None, help="覆盖相似度门限")
    parser.add_argument("--margin", type=float, default=None, help="覆盖边距")
    parser.add_argument("--quality-max-rms", type=float, default=None, help="覆盖质量门")
    parser.add_argument("--no-association", action="store_true", help="关掉关联层跑基线")
    parser.add_argument("--json", default=None, help="把统计写成 JSON")
    parser.add_argument("--quiet", action="store_true", help="只打结论")
    args = parser.parse_args(argv)

    def log(message):
        if not args.quiet:
            print(message, flush=True)

    doc, problems = load_annotation(args.video, args.annotations)
    if problems:
        for text in problems:
            print("[错误] %s" % text, file=sys.stderr)
        return 2

    cfg = config.load(args.profile)
    cfg["runtime"]["backend"] = args.backend
    cfg["runtime"]["max_fps"] = 0
    cfg["server"]["enabled"] = False
    cfg["output"]["save_snapshots"] = False
    cfg["output"]["print_events"] = False
    cfg["detection"]["model"] = _ascii_model_copy(
        config.resolve_vision(cfg["detection"]["model"]))
    cfg["runtime"]["detector_onnx"] = _ascii_model_copy(
        config.resolve_vision(cfg["runtime"]["detector_onnx"]))
    cfg["recognition"]["model"] = _ascii_model_copy(
        config.resolve_vision(cfg["recognition"]["model"]))
    assoc = dict(cfg["association"])
    assoc["enabled"] = not args.no_association
    if args.tau is not None:
        assoc["tau"] = args.tau
    if args.margin is not None:
        assoc["margin"] = args.margin
    if args.quality_max_rms is not None:
        assoc["quality_max_rms"] = args.quality_max_rms
    cfg["association"] = assoc

    log("[eval] %s（%s）tau=%.2f margin=%.2f 质量门 rms<=%.2f，stride=%d"
        % (os.path.basename(args.video), doc.get("scenario"), assoc["tau"],
           assoc["margin"], assoc["quality_max_rms"], args.stride))
    (reports, decisions, session_identities,
     processed, total_frames, _sink) = replay(
        args.video, doc, cfg, max(1, args.stride), log)

    identities = set()
    for row in doc.get("frames") or []:
        for face in row["faces"]:
            if face["identity"] != "?":
                identities.add(face["identity"])
    stats = summarize(doc.get("scenario") or "unknown", reports, identities)
    stats["video"] = os.path.basename(args.video)
    stats["processed_frames"] = processed
    stats["total_frames"] = total_frames
    stats["stride"] = args.stride
    stats["association"] = {"enabled": assoc["enabled"], "tau": assoc["tau"],
                            "margin": assoc["margin"],
                            "quality_max_rms": assoc["quality_max_rms"]}
    stats["cosine"] = cosine_stats(decisions, session_identities)
    stats["association_reasons"] = _reason_counts(decisions)
    stats["reports"] = reports
    stats["decisions"] = [
        row for row in decisions if row["reason"] != "track_bound"]
    print("[eval] 处理 %d/%d 帧；上报 %d 次（拦截 %d 次）；会话 %d 条；身份 %s"
          % (processed, total_frames, stats["events"], stats["suppressed"],
             stats["sessions"], stats["events_by_identity"]))
    print("[eval] 关联决策：%s" % stats["association_reasons"])
    if stats.get("cosine"):
        print("[eval] 余弦分布：%s" % json.dumps(stats["cosine"], ensure_ascii=False))
    if stats["passed"]:
        print("[eval] 断言通过：%s" % stats["scenario"])
    else:
        for text in stats["problems"]:
            print("[eval] 断言失败：%s" % text)
    if args.json:
        with open(args.json, "w", encoding="utf-8") as handle:
            json.dump(stats, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
        print("[eval] 统计 → %s" % args.json)
    return 0 if stats["passed"] else 1


if __name__ == "__main__":
    sys.exit(main())