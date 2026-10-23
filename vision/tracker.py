"""身份稳定：把"每帧的检测"变成"一次事件"（docs/02 §3.1 的发送时机）。

三件事都是协议硬要求，不是优化：
1. **连续 N 帧**才算确认（`confirm_frames`，默认 3）—— 避免瞬时误检刷屏
2. **同一 face_id 冷却**（`cooldown_sec`，默认 10s）—— 传输层去重
3. **同一 track 只发一次**（`emitted` 标记）—— 人站在门口不应重复上报

这里刻意**不依赖 cv2**：输入是普通 dict / 元组，输出是 `TrackEvent`，
所以跟踪逻辑能在没有摄像头、没有 OpenCV 的环境里被单测钉死。
"""
import time

DEFAULT_CONFIRM_FRAMES = 3
DEFAULT_COOLDOWN_SEC = 10.0
DEFAULT_TRACK_TTL_SEC = 2.0
DEFAULT_IOU = 0.3


class TrackEvent(object):
    """一次"值得上报"的人脸事件（还没变成 JSON）。"""

    __slots__ = ("face_id", "is_known", "name", "confidence", "bbox", "frame_ts", "track_id")

    def __init__(self, face_id, is_known, name, confidence, bbox, frame_ts, track_id):
        self.face_id = face_id
        self.is_known = bool(is_known)
        self.name = name
        self.confidence = float(confidence)
        self.bbox = tuple(int(v) for v in bbox)
        self.frame_ts = int(frame_ts)
        self.track_id = int(track_id)

    def __repr__(self):
        return "TrackEvent(%s, known=%s, name=%r, conf=%.2f)" % (
            self.face_id, self.is_known, self.name, self.confidence
        )


class _Track(object):
    __slots__ = ("id", "bbox", "first_ts", "last_ts", "votes", "meta", "unk_face_id", "emitted")

    def __init__(self, track_id, bbox, frame_ts, unk_face_id):
        self.id = track_id
        self.bbox = tuple(bbox)
        self.first_ts = int(frame_ts)
        self.last_ts = int(frame_ts)
        self.votes = {}      # 身份标签 -> 出现帧数
        self.meta = {}       # 身份标签 -> (is_known, name, best_score)
        self.unk_face_id = unk_face_id
        self.emitted = False


def iou(box_a, box_b):
    """交并比。box = (x, y, w, h)，像素坐标。"""
    ax, ay, aw, ah = box_a
    bx, by, bw, bh = box_b
    ax2, ay2 = ax + aw, ay + ah
    bx2, by2 = bx + bw, by + bh
    inter_w = max(0, min(ax2, bx2) - max(ax, bx))
    inter_h = max(0, min(ay2, by2) - max(ay, by))
    inter = float(inter_w * inter_h)
    if inter <= 0:
        return 0.0
    union = float(aw * ah + bw * bh) - inter
    return inter / union if union > 0 else 0.0


class FaceTracker(object):
    def __init__(self, confirm_frames=DEFAULT_CONFIRM_FRAMES,
                 cooldown_sec=DEFAULT_COOLDOWN_SEC,
                 track_ttl_sec=DEFAULT_TRACK_TTL_SEC,
                 iou_threshold=DEFAULT_IOU):
        self.confirm_frames = max(1, int(confirm_frames))
        self.cooldown_ms = int(float(cooldown_sec) * 1000)
        self.ttl_ms = int(float(track_ttl_sec) * 1000)
        self.iou_threshold = float(iou_threshold)
        self._tracks = []
        self._next_track_id = 1
        self._last_emit = {}   # face_id -> 上次上报的帧时刻
        self._unseen_ms = 0    # 最后一次 prune 丢掉的轨道数（调试用）

    # ---------------------------------------------------------------- 主入口

    def update(self, faces, frame_ts):
        """输入一帧的检测结果，返回**这一帧应该上报**的 `TrackEvent` 列表。

        `faces`: [{"bbox": (x, y, w, h),
                   "face_id": "p_10023" 或 None,
                   "name": "王阿姨" 或 None,
                   "score": 0.87}]
        """
        frame_ts = int(frame_ts)
        self._prune(frame_ts)

        unmatched = list(faces)
        matched = []   # (track, face)

        # 贪心 IoU 匹配：先配相似度最高的，避免两个挨着的人串轨
        pairs = []
        for face in unmatched:
            for track in self._tracks:
                score = iou(face["bbox"], track.bbox)
                if score >= self.iou_threshold:
                    pairs.append((score, id(track), track, face))
        pairs.sort(key=lambda item: item[0], reverse=True)

        used_faces = set()
        used_tracks = set()
        for _score, _key, track, face in pairs:
            if id(track) in used_tracks or id(face) in used_faces:
                continue
            used_tracks.add(id(track))
            used_faces.add(id(face))
            matched.append((track, face))

        for track, face in matched:
            track.bbox = tuple(face["bbox"])
            track.last_ts = frame_ts
            self._vote(track, face)

        for face in unmatched:
            if id(face) in used_faces:
                continue
            track = _Track(
                self._next_track_id, face["bbox"], frame_ts,
                self._new_unknown_id(frame_ts),
            )
            self._next_track_id += 1
            self._vote(track, face)
            self._tracks.append(track)

        return self._collect(frame_ts)

    # ---------------------------------------------------------------- 内部

    def _new_unknown_id(self, frame_ts):
        base = "unk_%d" % int(frame_ts)
        candidate = base
        suffix = 2
        taken = set(t.unk_face_id for t in self._tracks)
        while candidate in taken or candidate in self._last_emit:
            candidate = "%s_%d" % (base, suffix)
            suffix += 1
        return candidate

    def _vote(self, track, face):
        label = face.get("face_id") or track.unk_face_id
        track.votes[label] = track.votes.get(label, 0) + 1
        is_known = bool(face.get("face_id"))
        name = face.get("name") if is_known else None
        score = float(face.get("score") or 0.0)
        prev = track.meta.get(label)
        if prev is None or score > prev[2]:
            track.meta[label] = (is_known, name, score)

    def _collect(self, frame_ts):
        events = []
        for track in self._tracks:
            if track.emitted or not track.votes:
                continue
            label, count = max(track.votes.items(), key=lambda kv: kv[1])
            if count < self.confirm_frames:
                continue

            # 确认了：不管发不发，这条轨道到此为止，避免同一次停留重复上报
            track.emitted = True
            last = self._last_emit.get(label)
            if last is not None and (frame_ts - last) < self.cooldown_ms:
                continue
            self._last_emit[label] = frame_ts

            is_known, name, score = track.meta[label]
            events.append(TrackEvent(
                face_id=label, is_known=is_known, name=name, confidence=score,
                bbox=track.bbox, frame_ts=frame_ts, track_id=track.id,
            ))
        return events

    def _prune(self, frame_ts):
        alive = []
        for track in self._tracks:
            if frame_ts - track.last_ts <= self.ttl_ms:
                alive.append(track)
            else:
                self._unseen_ms = frame_ts
        self._tracks = alive

    def reset(self):
        """清空轨道与冷却记录（测试和"重新开始一次演示"用）。"""
        self._tracks = []
        self._last_emit = {}
        self._next_track_id = 1

    @property
    def track_count(self):
        return len(self._tracks)
