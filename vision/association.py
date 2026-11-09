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

2026-11-08 真素材复评（`docs/真实素材关联复评-2026-11-08.md`）加了一条
**候选挂起**语义：弱证据（低于 tau / 歧义 / 会话被占）第一次只返回
`pending_evidence` 不建会话，同一轨道连续 `weak_obs` 帧仍弱才落成新会话。
原因见 `DEFAULT_WEAK_OBS` 的注释。
"""
import math

DEFAULT_TAU = 0.45
DEFAULT_MARGIN = 0.10
DEFAULT_QUALITY_MAX_RMS = 0.10
DEFAULT_SESSION_TTL_SEC = 120.0
DEFAULT_TRACK_TTL_SEC = 2.0
DEFAULT_MAX_SESSIONS = 64
DEFAULT_MAX_REFS = 8
# 弱证据（低于 tau / 歧义 / 会话被占）要连续观察到几次才建会话。
# 一次就走位/侧脸的单帧证据多半是"同一个人出画"的残影，建会话会让之后的正脸
# 全灌进这条重复会话，同一个人被拆成两条会话互相比拼 → 重报（2026-11-08 sc01 实测）。
DEFAULT_WEAK_OBS = 2

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
                 max_refs=DEFAULT_MAX_REFS,
                 weak_obs=DEFAULT_WEAK_OBS):
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
        if int(weak_obs) < 1:
            raise ValueError("weak_obs 至少为 1")

        self.enabled = bool(enabled)
        self.tau = tau
        self.margin = margin
        self.quality_max_rms = float(quality_max_rms)
        self.session_ttl_ms = int(float(session_ttl_sec) * 1000)
        self.track_ttl_ms = int(float(track_ttl_sec) * 1000)
        self.max_sessions = int(max_sessions)
        self.max_refs = int(max_refs)
        self.weak_obs = int(weak_obs)
        self._sessions = {}        # session_id -> _Session
        self._by_track = {}        # track_id -> _Session（轨道当前绑定）
        self._pending = {}         # track_id -> {"count": n, "frame_ts": ts}
        self._next_session_id = 1

    # ---------------------------------------------------------------- 主入口

    def observe(self, track_id, embedding, rms_norm, frame_ts, tracked_ids=None):
        """喂一条证据（来自某个已确认轨道），返回关联判定。

        `track_id`：空间轨道 id；`embedding`：L2 归一化前后的 128 维特征都可；
        `rms_norm`：该脸的五点对齐残差 ÷ 眼距；没有质量指标时传 None。
        `tracked_ids`：**这一帧在场**的轨道 id 集合。给了它，就能区分「两个人同时在」
        和「换轨交接」：前者不许共享会话，后者（旧轨道这一帧已经不在场）允许交接。
        不给则退回纯 TTL 判据（老行为，单测/离线回放用）。
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
            # 会话寿命按"最后一次看到证据"算：连续站着的人不该被 TTL 判过期
            bound.last_ts = frame_ts
            self._enroll(bound, vec, rms_norm)
            self._pending.pop(int(track_id), None)
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
        # 轨道真的换了会话时再把绑定改过去：沿轨分支不能提前改，否则下一帧永远
        # "沿轨"、永远拿不到比较值，首报延迟就只会按时间兜底放行（2026-11-09 实测）。
        weak_reason = None
        if best is None:
            # 没有任何可比的会话：不存在"像谁"的歧义，直接建
            self._pending.pop(int(track_id), None)
            return self._new_session(track_id, vec, rms_norm, frame_ts, "new_session")
        if best_score < self.tau:
            weak_reason = "below_tau"
        elif other_best is not None and (best_score - other_best) < self.margin:
            weak_reason = "ambiguous_margin"
        elif self._is_occupied_by_other(best, track_id, frame_ts, tracked_ids):
            weak_reason = "session_occupied"

        if weak_reason is not None:
            # 弱证据先挂起：一帧就走位/侧脸的残影不建会话，否则正脸会灌进重复会话，
            # 同一个人被拆成两条会话 → 之后互相比拼、margin 不够 → 重报。
            pending = self._pending.get(int(track_id))
            if pending is not None and (frame_ts - pending["frame_ts"]) <= self.track_ttl_ms:
                pending["count"] += 1
                pending["frame_ts"] = frame_ts
            else:
                pending = {"count": 1, "frame_ts": frame_ts}
                self._pending[int(track_id)] = pending
            if pending["count"] < self.weak_obs:
                return AssociationResult(None, False, "pending_evidence",
                                         best_score, other_best)
            self._pending.pop(int(track_id), None)
            return self._new_session(track_id, vec, rms_norm, frame_ts, weak_reason,
                                     best_score, other_best)

        best.track_id = int(track_id)
        best.track_ts = frame_ts
        best.last_ts = frame_ts
        self._by_track[int(track_id)] = best
        self._enroll(best, vec, rms_norm)
        self._pending.pop(int(track_id), None)
        return AssociationResult(best.id, True, "associated", best_score, other_best)

    def reset(self):
        """清空所有会话与轨道绑定（测试 / 重新开始一次演示）。"""
        self._sessions = {}
        self._by_track = {}
        self._pending = {}
        self._next_session_id = 1

    def drop_session(self, session_id):
        """撤掉一条会话（连同轨道绑定与挂起证据）。

        服务层用它撤销"没撑住首报确认"的会话：那条会话是出画残影临时建出来的，
        留着会让下一帧同一张脸的弱证据与它比较、继续把参考库灌进去。
        """
        if session_id is None:
            return False
        session = self._sessions.pop(int(session_id), None)
        if session is None:
            return False
        for track_id in list(self._by_track.keys()):
            if self._by_track[track_id] is session:
                self._by_track.pop(track_id, None)
        return True

    def created_by(self, session_id, track_id, frame_ts):
        """这条会话是不是"此刻由这条轨道刚建出来的"。

        服务的首报延迟用这个判据区分两种 below_tau：一种是"看不出来"，
        一种是时段边界上恰好落在 below_tau、但候选已经由视觉层自己攒够 weak_obs
        建出来了 —— 后者该延后确认，不该当残影直接否决。
        关联器的 `pending_evidence` 不建会话，所以这个信号不会被普通弱证据触发。
        """
        if session_id is None:
            return False
        session = self._sessions.get(int(session_id))
        if session is None:
            return False
        return (session.track_id == int(track_id)
                and int(session.created_ts) == int(frame_ts))

    # ---------------------------------------------------------------- 内部

    def _is_occupied_by_other(self, session, track_id, frame_ts, tracked_ids=None):
        """会话是否被另一条**还在活跃**的轨道占着（一人一轨，不许两人共享）。

        2026-11-07 板上回放补充：点位框抖动会让 tracker 重建轨道（换轨），旧轨道在
        TTL 内还"活着"但不匹配任何检测。纯 TTL 判据会把这种情况当成"另一个人来抢" →
        新建重复会话 → 后续相似度歧义 → 事件数反弹。给了 `tracked_ids` 时，只要占位
        轨道**这一帧不在场**，就当作换轨交接，允许复用同一条会话。
        """
        if session.track_id is None or session.track_id == int(track_id):
            return False
        if tracked_ids is not None and int(session.track_id) not in tracked_ids:
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
        for track_id in list(self._pending.keys()):
            if (frame_ts - self._pending[track_id]["frame_ts"]) > self.track_ttl_ms:
                self._pending.pop(track_id, None)

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