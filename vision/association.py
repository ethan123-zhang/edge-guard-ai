"""短期匿名关联层：把"新空间轨道"和"已见过的匿名会话"用外观证据连起来。

合同见 `docs/跨轨关联与两层去重合同-2026-11-01.md`。核心只有一条：
**证据充分且唯一时才复用会话，否则新建独立候选。** 不靠框接近、同侧、
同数量或时间短来合并 —— 那些线索分不清"原人恢复"和"同位置换人"。

数字来自 2026-11-06 的两组研究（`docs/质量筛选与保守参考策略-2026-11-06.md`）：
- 入册质量门 `rms_norm <= 0.10`：五点对齐残差 ÷ 眼距（无量纲），只把高质量
  脸写进参考库；入册比例约 5%，但能把两个身份在参考库里分开。
- 证据阈值 `tau=0.45`、边距 `delta=0.10`：本方相似度 >= tau，且
  （本方 - 异方）>= delta 才复用会话。LFW 500 个异人对上 FAR=0。

本模块刻意**不依赖 cv2 / numpy**：输入是普通 list / tuple，输出是
`AssociationResult`，所以关联逻辑能在没有 OpenCV 的环境里被单测钉死。
也不保存人脸图像、身份库或永久人员键；特征只驻留内存，随会话过期。
"""
import math

DEFAULT_TAU = 0.45
DEFAULT_MARGIN = 0.10
DEFAULT_QUALITY_MAX_RMS = 0.10
DEFAULT_SESSION_TTL_SEC = 120.0
DEFAULT_TRACK_TTL_SEC = 2.0
DEFAULT_MAX_SESSIONS = 64
DEFAULT_MAX_REFS = 8

# 余弦计算的最小范数：低于它的向量视为无效证据（零向量 / 损坏特征）
_MIN_NORM = 1e-9


def _norm(vec):
    total = 0.0
    for value in vec:
        total += float(value) * float(value)
    return math.sqrt(total)


def _cosine(a, b):
    na = _norm(a)
    nb = _norm(b)
    if na <= _MIN_NORM or nb <= _MIN_NORM:
        return None
    dot = 0.0
    for x, y in zip(a, b):
        dot += float(x) * float(y)
    return dot / (na * nb)


def _as_vector(embedding):
    """把外部输入收敛成 float 元组；长度为 0 或含非有限值时返回 None。"""
    if embedding is None:
        return None
    try:
        vec = tuple(float(v) for v in embedding)
    except (TypeError, ValueError):
        return None
    if not vec:
        return None
    for value in vec:
        if value != value or value in (float("inf"), float("-inf")):
            return None
    return vec


class AssociationResult(object):
    """一次关联判定的结果与理由（诊断只留相对时间/标识/理由，不留特征）。"""

    __slots__ = ("session_id", "associated", "reason", "own_best", "other_best")

    def __init__(self, session_id, associated, reason, own_best=None, other_best=None):
        self.session_id = session_id
        self.associated = bool(associated)
        self.reason = reason
        self.own_best = own_best
        self.other_best = other_best

    @property
    def margin(self):
        if self.own_best is None or self.other_best is None:
            return None
        return self.own_best - self.other_best

    def __repr__(self):
        return "AssociationResult(session=%s, associated=%s, reason=%s, own=%r, other=%r)" % (
            self.session_id, self.associated, self.reason, self.own_best, self.other_best)


class _Session(object):
    """一个匿名会话：生命周期独立于空间轨道，容纳若干已入册的高质量参考特征。"""

    __slots__ = ("id", "created_ts", "last_ts", "refs", "track_id", "track_ts")

    def __init__(self, session_id, frame_ts, track_id):
        self.id = session_id
        self.created_ts = int(frame_ts)
        self.last_ts = int(frame_ts)
        self.refs = []            # [(vec, rms_norm), ...]，只含过质量门的脸
        self.track_id = track_id  # 当前占用的轨道（一对一）
        self.track_ts = int(frame_ts)


class AnonymousAssociator(object):
    """短期匿名关联器。默认关闭；打开后也只复用"证据唯一"的会话。"""

    def __init__(self, enabled=False, tau=DEFAULT_TAU, margin=DEFAULT_MARGIN,
                 quality_max_rms=DEFAULT_QUALITY_MAX_RMS,
                 session_ttl_sec=DEFAULT_SESSION_TTL_SEC,
                 track_ttl_sec=DEFAULT_TRACK_TTL_SEC,
                 max_sessions=DEFAULT_MAX_SESSIONS,
                 max_refs=DEFAULT_MAX_REFS):
        tau = float(tau)
        margin = float(margin)
        if not (0.0 < tau <= 1.0):
            raise ValueError("tau 必须在 (0, 1] 之间：%r" % tau)
        if margin < 0.0:
            raise ValueError("margin 不能为负：%r" % margin)
        if float(quality_max_rms) < 0.0:
            raise ValueError("quality_max_rms 不能为负：%r" % quality_max_rms)
        if float(session_ttl_sec) <= 0.0 or float(track_ttl_sec) <= 0.0:
            raise ValueError("session_ttl_sec / track_ttl_sec 必须为正")
        if int(max_sessions) < 1 or int(max_refs) < 1:
            raise ValueError("max_sessions / max_refs 至少为 1")

        self.enabled = bool(enabled)
        self.tau = tau
        self.margin = margin
        self.quality_max_rms = float(quality_max_rms)
        self.session_ttl_ms = int(float(session_ttl_sec) * 1000)
        self.track_ttl_ms = int(float(track_ttl_sec) * 1000)
        self.max_sessions = int(max_sessions)
        self.max_refs = int(max_refs)
        self._sessions = {}        # session_id -> _Session
        self._by_track = {}        # track_id -> _Session（轨道当前绑定）
        self._next_session_id = 1

    # ---------------------------------------------------------------- 主入口

    def observe(self, track_id, embedding, rms_norm, frame_ts):
        """喂一条证据（来自某个已确认轨道），返回关联判定。

        `track_id`：空间轨道 id；`embedding`：L2 归一化前后的 128 维特征都可；
        `rms_norm`：该脸的五点对齐残差 ÷ 眼距；没有质量指标时传 None。
        """
        frame_ts = int(frame_ts)
        if not self.enabled:
            return AssociationResult(None, False, "disabled")
        vec = _as_vector(embedding)
        if vec is None or _norm(vec) <= _MIN_NORM:
            return AssociationResult(None, False, "invalid_evidence")
        self._prune(frame_ts)

        # 同一轨道还在活跃期：沿用已绑定的会话，只补参考库
        bound = self._by_track.get(track_id)
        if bound is not None and (frame_ts - bound.track_ts) <= self.track_ttl_ms:
            bound.track_ts = frame_ts
            self._enroll(bound, vec, rms_norm)
            return AssociationResult(bound.id, True, "track_bound")

        # 找最像的会话，同时记录第二像的（用于"唯一性"判断）
        best = None
        best_score = None
        other_score = None
        for session in self._sessions.values():
            score = self._best_similarity(vec, session)
            if score is None:
                continue
            if best_score is None or score > best_score:
                other_score = best_score
                best_score = score
                best = session
            elif other_score is None or score > other_score:
                other_score = score
        other_best = other_score

        if best is None:
            return self._new_session(track_id, vec, rms_norm, frame_ts, "new_session")
        if best_score < self.tau:
            return self._new_session(track_id, vec, rms_norm, frame_ts, "below_tau",
                                     best_score, other_best)
        if other_best is not None and (best_score - other_best) < self.margin:
            return self._new_session(track_id, vec, rms_norm, frame_ts, "ambiguous_margin",
                                     best_score, other_best)
        if self._is_occupied_by_other(best, track_id, frame_ts):
            return self._new_session(track_id, vec, rms_norm, frame_ts, "session_occupied",
                                     best_score, other_best)

        best.track_id = int(track_id)
        best.track_ts = frame_ts
        best.last_ts = frame_ts
        self._by_track[int(track_id)] = best
        self._enroll(best, vec, rms_norm)
        return AssociationResult(best.id, True, "associated", best_score, other_best)

    def reset(self):
        """清空所有会话与轨道绑定（测试 / 重新开始一次演示）。"""
        self._sessions = {}
        self._by_track = {}
        self._next_session_id = 1

    # ---------------------------------------------------------------- 内部

    def _is_occupied_by_other(self, session, track_id, frame_ts):
        if session.track_id is None or session.track_id == int(track_id):
            return False
        return (frame_ts - session.track_ts) <= self.track_ttl_ms

    def _best_similarity(self, vec, session):
        best = None
        for ref, _rms in session.refs:
            score = _cosine(vec, ref)
            if score is not None and (best is None or score > best):
                best = score
        return best

    def _new_session(self, track_id, vec, rms_norm, frame_ts, reason,
                     best_score=None, other_best=None):
        session = _Session(self._next_session_id, frame_ts, track_id)
        self._next_session_id += 1
        self._enroll(session, vec, rms_norm)
        self._sessions[session.id] = session
        self._by_track[int(track_id)] = session
        self._evict_if_needed()
        return AssociationResult(session.id, False, reason, best_score, other_best)

    def _enroll(self, session, vec, rms_norm):
        """只把过质量门的脸写进参考库；库满时用质量更好的替换最差的。"""
        if rms_norm is None:
            return
        try:
            rms = float(rms_norm)
        except (TypeError, ValueError):
            return
        if rms != rms or rms > self.quality_max_rms:
            return
        if len(session.refs) < self.max_refs:
            session.refs.append((vec, rms))
            return
        worst_index = 0
        for index in range(1, len(session.refs)):
            if session.refs[index][1] > session.refs[worst_index][1]:
                worst_index = index
        if rms < session.refs[worst_index][1]:
            session.refs[worst_index] = (vec, rms)

    def _prune(self, frame_ts):
        for session_id in list(self._sessions.keys()):
            session = self._sessions[session_id]
            if (frame_ts - session.last_ts) > self.session_ttl_ms:
                self._sessions.pop(session_id, None)
                if self._by_track.get(session.track_id) is session:
                    self._by_track.pop(session.track_id, None)
        for track_id in list(self._by_track.keys()):
            session = self._by_track[track_id]
            if session.id not in self._sessions:
                self._by_track.pop(track_id, None)

    def _evict_if_needed(self):
        while len(self._sessions) > self.max_sessions:
            oldest = min(self._sessions.values(), key=lambda s: (s.last_ts, s.id))
            self._sessions.pop(oldest.id, None)
            if self._by_track.get(oldest.track_id) is oldest:
                self._by_track.pop(oldest.track_id, None)

    # ---------------------------------------------------------------- 只读

    @property
    def session_count(self):
        return len(self._sessions)

    def session_ids(self):
        return sorted(self._sessions.keys())