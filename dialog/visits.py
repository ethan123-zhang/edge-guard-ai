"""业务层第 1 层：10 分钟到访窗口（把 face_event 归并成 visit）。

**为什么不在视觉层做**：视觉层只负责"这次看到的人要不要发一条事件"（10 秒冷却、
匿名会话去重）；"这条事件算不算同一次到访"是业务判断，需要人员身份键和到访账本。
两层混在一起，视觉节点就没法离线独立回归，阈值也会互相污染。

语义（docs/02 §3.1/§4.1，docs/跨轨关联与两层去重合同 §业务窗口的建议语义）：

1. **固定窗口**：以该次到访的 `arrived_at` 为起点，`0 <= frame_ts - arrived_at < 600000ms`
   内算同一次；恰好 600000ms 另建一次。后续事件**不会**把窗口滑动延长
   —— 否则同一人每 9 分钟来一次，就永远不会被记成"又来了一次"。
2. **活跃到访一律并入**：`departed_at IS NULL` 且 `completed = 0` 的到访即使已经
   在场超过 10 分钟，同人新事件也并入 —— 不能拿计时把"站了很久的一个人"续成第二次到访。
3. **陌生人不跨 ID 互并**：每个 `unk_*` 各走各的账；没有视觉层关联证据就不猜
   （合同要求"缺少关联时不得跨 unk_ID 套用同人条件"）。
4. **msg_id 幂等**：同一条消息重复投递（WS 重连/重传）只处理一次，账本落
   `visit_event` 表，跨进程重启也有效。

**边界（不要在这里补）**：离场判定（写 `departed_at`）不归本模块管 ——
合同明确它要另立协议/合同（当前 face_event 没有"人走了"的信号，编出来的
departed_at 是假数据）。所以长期未收口的到访会一直被当成活跃到访，这是已知限制。
"""
from __future__ import annotations

from typing import Any, Dict, Optional, Tuple

from dialog import db

#: 到访去重窗口（分钟，docs/02 §7.2 `dialog.visit_dedup_min` 的默认值）
WINDOW_MIN = 10
WINDOW_MS = WINDOW_MIN * 60 * 1000

#: face_event 的必填字段（docs/02 §3.1，与 vision/events.py 对齐）
FACE_EVENT_REQUIRED = ("msg_id", "face_id", "is_known", "name", "confidence", "frame_ts")

#: 决策理由：现场/回放时靠它回答"为什么算同一次"
REASON_NEW = "new_visit"                # 新建一次到访
REASON_ACTIVE = "reuse_active"          # 并入仍在进行中的到访
REASON_WINDOW = "reuse_window"          # 上一次到访已收口，但还在 10 分钟窗口内
REASON_REPLAY = "replay_msg"            # 同 msg_id 重复投递，没有产生新动作


class VisitDecision(object):
    """一次 `face_event` 的处理结果。"""

    __slots__ = ("visit_id", "created", "reason", "person_visit_count")

    def __init__(self, visit_id: str, created: bool, reason: str,
                 person_visit_count: int = 0):
        self.visit_id = str(visit_id)
        self.created = bool(created)
        self.reason = str(reason)
        #: **含本次**的累计到访次数（docs/02 §4.1：第 1 次来时值为 1）
        self.person_visit_count = int(person_visit_count)

    def __repr__(self):
        return "VisitDecision(%s, created=%s, reason=%s, count=%d)" % (
            self.visit_id, self.created, self.reason, self.person_visit_count)


class VisitBook(object):
    """到访账本：把 `face_event` 归并成 `visit` 行。

    典型用法（对话节点收到一条已通过协议校验的事件）::

        book = VisitBook(conn, home_state="empty")
        decision = book.arrive(msg)
        if decision.created:
            ...启动迎宾对话...
    """

    def __init__(self, conn, window_ms: int = WINDOW_MS, home_state: str = "empty",
                 log=None):
        window_ms = int(window_ms)
        if window_ms <= 0:
            raise ValueError("window_ms 必须是正整数（毫秒），收到 %r" % (window_ms,))
        self.conn = conn
        self.window_ms = window_ms
        self.home_state = str(home_state)
        self._log = log if log is not None else (lambda message: None)

    # ---------------------------------------------------------------- 主入口

    def arrive(self, msg: Dict[str, Any], home_state: Optional[str] = None) -> VisitDecision:
        """处理一条 face_event，返回它归到哪次到访。

        只做"归并与记账"：开对话、抽槽位、发 report 都是调用方的事。
        """
        face_id, is_known, name, confidence, frame_ts, msg_id = parse_face_event(msg)
        home = self.home_state if home_state is None else str(home_state)

        replay = db.get_visit_event(self.conn, msg_id)
        if replay is not None:
            if str(replay["face_id"]) != face_id:
                # 同一个 msg_id 挂着两个人：账本已经被污染，不能静默当成重传
                raise ValueError("msg_id %s 重复但 face_id 不一致（账本 %s，本次 %s）"
                                 % (msg_id, replay["face_id"], face_id))
            # 重传：账本里已经有这条消息，任何计数都不许动
            person = db.get_person(self.conn, face_id)
            count = int(person["visit_count"]) if person is not None else 0
            self._log("[visit] 重复消息 msg_id=%s（已计入 %s），忽略"
                      % (msg_id, replay["visit_id"]))
            return VisitDecision(replay["visit_id"], False, REASON_REPLAY, count)

        # 来人登场：last_seen 更新、已知人脸补名字；visit_count 由 count_visit 累加
        db.upsert_person(self.conn, face_id=face_id, is_known=is_known,
                         now_ms=frame_ts, name=name)

        latest = db.latest_visit_for(self.conn, face_id)
        reason = self._reuse_reason(latest, frame_ts) if latest is not None else None
        if reason is not None:
            visit_id = str(latest["visit_id"])
            created = False
            self._log("[visit] %s 并入 %s（%s，frame_ts=%d）"
                      % (face_id, visit_id, reason, frame_ts))
        else:
            visit_id = db.create_visit(
                self.conn, face_id=face_id, arrived_at=frame_ts, home_state=home,
                is_known=is_known, confidence=confidence, now_ms=frame_ts,
            )
            # 「第 N 次来」在问候时就要用，所以到访一建立就计数（docs/02 §4.1 含本次）
            db.count_visit(self.conn, face_id)
            created = True
            self._log("[visit] %s 新建 %s（frame_ts=%d）" % (face_id, visit_id, frame_ts))

        db.log_visit_event(self.conn, msg_id=msg_id, visit_id=visit_id,
                           face_id=face_id, frame_ts=frame_ts)
        person = db.get_person(self.conn, face_id)
        count = int(person["visit_count"]) if person is not None else 0
        return VisitDecision(visit_id, created, reason or REASON_NEW, count)

    # ---------------------------------------------------------------- 内部

    def _reuse_reason(self, latest, frame_ts: int) -> Optional[str]:
        """上一次到访能不能接住这条新事件；不能返回 None（要新建）。"""
        if latest["departed_at"] is None and not int(latest["completed"] or 0):
            return REASON_ACTIVE
        delta = int(frame_ts) - int(latest["arrived_at"])
        if delta < self.window_ms:
            # 负数 = 乱序/延迟送达的旧事件，也归到那次到访
            return REASON_WINDOW
        return None


def parse_face_event(msg: Dict[str, Any]) -> Tuple[str, bool, Optional[str], float, int, str]:
    """校验并取出 `(face_id, is_known, name, confidence, frame_ts, msg_id)`。

    严格到字段缺失就抛 `ValueError`：业务层不许猜——少个 `frame_ts` 就归错过期窗口，
    少个 `msg_id` 就做不了幂等。错误信息写清缺了什么，方便对端改协议。
    """
    if not isinstance(msg, dict):
        raise ValueError("face_event 必须是 dict，收到 %r" % (type(msg).__name__,))
    if msg.get("type") != "face_event":
        raise ValueError("不是 face_event（type=%r）" % (msg.get("type"),))
    missing = [key for key in FACE_EVENT_REQUIRED if key not in msg]
    if missing:
        raise ValueError("face_event 缺字段：%s" % "、".join(missing))

    face_id = msg["face_id"]
    msg_id = msg["msg_id"]
    if not isinstance(face_id, str) or not face_id:
        raise ValueError("face_id 必须是非空字符串，收到 %r" % (face_id,))
    if not isinstance(msg_id, str) or not msg_id:
        raise ValueError("msg_id 必须是非空字符串，收到 %r" % (msg_id,))

    name = msg["name"]
    if name is not None and not isinstance(name, str):
        raise ValueError("name 必须是 str 或 None，收到 %r" % (name,))
    if not isinstance(msg["is_known"], bool):
        raise ValueError("is_known 必须是 bool，收到 %r（不要传 0/1）" % (msg["is_known"],))
    is_known = msg["is_known"]
    if is_known and not name:
        raise ValueError("is_known=true 时必须带 name（docs/02 §3.1）")

    try:
        frame_ts = int(msg["frame_ts"])
    except (TypeError, ValueError):
        raise ValueError("frame_ts 必须是毫秒整数，收到 %r" % (msg["frame_ts"],))
    try:
        confidence = float(msg["confidence"])
    except (TypeError, ValueError):
        raise ValueError("confidence 必须是数字，收到 %r" % (msg["confidence"],))
    return face_id, is_known, name, confidence, frame_ts, msg_id