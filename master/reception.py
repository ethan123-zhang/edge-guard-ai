"""接待会话：规则优先于模型，汇总只发一次。

这一段是安全关键（设计 §4）。顺序**钉死**，不许换：

1. **幂等**：`(visit_id, utterance_id)` 重复投递只返回既有结果，不重复入队、不重复计数。
2. **越权规则先跑**：`privilege.detect()` 命中 → 立刻入队 `security_alert`
   （不等收尾、不等照片），写 `visit.privileged=1`，回复用固定话术，**不经模型**。
3. **再抽来意**：只有明确说明才算来意（"我来送快递"）；"你好""把门打开"不算。
   来访者**自称**的姓名单独存 `caller_name`，**不**据此认定熟人。
4. **汇总只生成一次**：收口时若明确来意已成立，以 `report:<visit_id>` 入队一条，
   payload 冻结；后面的输入只更新 `visit` 行，不重建消息（任务书 §2.5）。
5. **收口动作**：正常结束 / 沉默超时 / 接待超时三种。`departed_at` 只在有离场证据时写
   —— 当前 `face_event` 没有离场信号，所以**不写**（材料里如实说明）。

`tick()` 由 server 的后台线程定时调用，负责沉默/接待超时收口。
"""
from __future__ import annotations

import re
import time
from typing import Any, Callable, Dict, List, Optional, Tuple

from dialog import db, phrases, privilege
from dialog.visits import VisitBook, parse_face_event
from master import store

REASON_FAREWELL = "farewell"          # 正常结束：访客明确说收尾
REASON_SILENCE = "silence_timeout"    # 沉默超时
REASON_SESSION = "session_timeout"    # 接待超时

#: 明确来意的关键词表（确定性规则；设计 §4.3 "只有明确说明才算来意"）
PURPOSE_RULES: Tuple[Tuple[str, Tuple[str, ...]], ...] = (
    ("送货", ("快递", "包裹", "送货", "送东西", "送个东西", "放门口", "外卖", "送文件", "送东西过来")),
    ("找人", ("找人", "找一下", "拜访", "来看看", "找他", "找她", "找老王")),
    ("维修", ("维修", "修一下", "检修", "上门修", "安装")),
    ("推销", ("推销", "推广", "办卡", "卖东西", "做个业务")),
)

URGENT_KEYWORDS = ("急", "马上", "赶紧", "紧急", "尽快")

#: 自称姓名（只在明确自报时取值；不做模糊人名识别）
_NAME_PATTERNS = (
    re.compile(r"我姓([\u4e00-\u9fa5]{1,2})"),
    re.compile(r"我叫([\u4e00-\u9fa5]{1,4})"),
    re.compile(r"我是([\u4e00-\u9fa5]{1,4}(?:先生|女士|师傅|老师))"),
)


def extract_intent(text: str) -> Dict[str, Any]:
    """从一句话里抽 `purpose / caller_name / urgent`；没明确说明就是 `None`。"""
    raw = text or ""
    purpose = None
    for category, keywords in PURPOSE_RULES:
        if any(keyword in raw for keyword in keywords):
            purpose = category
            break

    caller_name = None
    for pattern in _NAME_PATTERNS:
        match = pattern.search(raw)
        if match:
            candidate = match.group(1)
            if match.re is _NAME_PATTERNS[0]:
                candidate = candidate + "先生"
            caller_name = candidate
            break

    urgent = any(keyword in raw for keyword in URGENT_KEYWORDS)
    return {"purpose": purpose, "caller_name": caller_name, "urgent": urgent}


def is_farewell(text: str, cfg: Dict[str, Any]) -> bool:
    phrases_cfg = cfg.get("reception") or {}
    for phrase in phrases_cfg.get("farewell_phrases", []):
        if phrase and phrase in (text or ""):
            return True
    return False


class ReceptionService(object):
    """接待主循环。所有公开方法都假定调用方已持有数据库锁。"""

    def __init__(self, conn, outbox, cfg: Dict[str, Any],
                 now_fn: Optional[Callable[[], int]] = None,
                 log: Optional[Callable[[str], None]] = None,
                 notify: Optional[Callable[[Dict[str, Any]], None]] = None):
        self.conn = conn
        self.outbox = outbox
        self.cfg = cfg
        self._now = now_fn or (lambda: int(time.time() * 1000))
        self._log = log or (lambda message: None)
        self._notify = notify or (lambda payload: None)
        self._home_state = "empty"
        self.sessions: Dict[str, Dict[str, Any]] = {}
        reception_cfg = cfg.get("reception") or {}
        self.silence_ms = int(reception_cfg.get("silence_timeout_ms", 45000))
        self.session_ms = int(reception_cfg.get("session_timeout_ms", 300000))

    # ---------------------------------------------------------------- 家状态

    def set_home_state(self, state: str) -> str:
        state = "occupied" if str(state) == "occupied" else "empty"
        self._home_state = state
        db.log_event(self.conn, level="info", category="state",
                     message="家中状态切换", detail={"home_state": state})
        return state

    @property
    def home_state(self) -> str:
        return self._home_state

    # ---------------------------------------------------------------- 人来

    def on_face_event(self, msg: Dict[str, Any]) -> Dict[str, Any]:
        """处理一条视觉事件：归并到访 + 首次问候。"""
        face_id, is_known, name, confidence, frame_ts, msg_id = parse_face_event(msg)
        book = VisitBook(self.conn, home_state=self._home_state)
        decision = book.arrive(msg, home_state=self._home_state)

        if decision.reason == "replay_msg":
            return {"visit_id": decision.visit_id, "created": False,
                    "replayed": True, "reason": decision.reason,
                    "greeted": False, "face_id": face_id, "frame_ts": frame_ts}

        session = self.sessions.get(decision.visit_id)
        greeted = False
        if session is None:
            session = {"started_at": frame_ts, "last_activity": frame_ts,
                       "greeted": False, "closed": False, "reasons": []}
            self.sessions[decision.visit_id] = session

        if not session["greeted"]:
            count = decision.person_visit_count
            display_name = name if is_known else None
            greeting = phrases.greeting(display_name, count)
            store.add_turn(self.conn, decision.visit_id, "assistant", greeting,
                           ts=frame_ts)
            session["greeted"] = True
            greeted = True
            db.log_event(self.conn, level="info", category="reception",
                         message="开始接待", detail={"visit_id": decision.visit_id,
                                                      "face_id": face_id})
            self._log("[reception] %s 开始接待（%s）" % (decision.visit_id, decision.reason))

        session["last_activity"] = max(int(session["last_activity"]), int(frame_ts))
        return {"visit_id": decision.visit_id, "created": decision.created,
                "replayed": False, "reason": decision.reason, "greeted": greeted,
                "face_id": face_id, "frame_ts": frame_ts,
                "count": decision.person_visit_count}

    # ---------------------------------------------------------------- 访客说话

    def on_utterance(self, visit_id: str, text: str, utterance_id: str,
                     source: str = "keyboard") -> Dict[str, Any]:
        """接待入口：键盘现在用；天问51 将来发同一入口（source="twen51"）。"""
        if not isinstance(text, str) or not text.strip():
            raise ValueError("text 不能为空")
        if not isinstance(utterance_id, str) or not utterance_id.strip():
            raise ValueError("utterance_id 必填（网络重试必须复用同一个）")
        visit = db.get_visit(self.conn, visit_id)
        if visit is None:
            raise ValueError("到访不存在：%s" % visit_id)

        existing = store.find_turn(self.conn, visit_id, utterance_id)
        if existing is not None:
            return self._replay_result(visit_id, str(existing["id"]))

        text = text.strip()
        if not store.add_turn(self.conn, visit_id, "user", text,
                              utterance_id=utterance_id, ts=self._now()):
            # 并发下唯一索引兜底：另一线程已经处理过这条
            row = store.find_turn(self.conn, visit_id, utterance_id)
            return self._replay_result(visit_id, str(row["id"]) if row else "")

        session = self.sessions.get(visit_id)
        if session is None:
            session = {"started_at": self._now(), "last_activity": self._now(),
                       "greeted": True, "closed": False, "reasons": []}
            self.sessions[visit_id] = session
        session["last_activity"] = self._now()

        rounds = self._user_turn_count(visit_id)
        store.set_visit_slots(self.conn, visit_id, rounds=rounds)

        detection = privilege.detect(text)
        if detection.hit:
            return self._handle_privilege(visit, visit_id, text, utterance_id,
                                          detection, source)

        intent = extract_intent(text)
        purpose = intent["purpose"]
        if purpose:
            store.set_visit_slots(self.conn, visit_id, purpose=purpose)
        if intent["caller_name"]:
            store.set_visit_slots(self.conn, visit_id, caller_name=intent["caller_name"])

        farewell = is_farewell(text, self.cfg)
        reply = self._reply_for(intent, farewell)
        store.add_turn(self.conn, visit_id, "assistant", reply, ts=self._now())

        result = {"visit_id": visit_id, "reply": reply, "alert": False,
                  "purpose": purpose, "caller_name": intent["caller_name"],
                  "farewell": farewell, "replayed": False, "source": source}
        if farewell:
            result["closed"] = self.close(visit_id, REASON_FAREWELL, completed=True)
        else:
            result["closed"] = False
        return result

    # ---------------------------------------------------------------- 收口

    def tick(self, now: Optional[int] = None) -> List[Dict[str, Any]]:
        """后台线程定时调用：沉默超时 / 接待超时收口。返回本轮收口的到访。"""
        ts = self._now() if now is None else int(now)
        closed = []
        for visit_id, session in list(self.sessions.items()):
            if session.get("closed"):
                continue
            idle = ts - int(session.get("last_activity", ts))
            alive = ts - int(session.get("started_at", ts))
            if idle >= self.silence_ms:
                closed.append(self.close(visit_id, REASON_SILENCE, completed=False))
            elif alive >= self.session_ms:
                closed.append(self.close(visit_id, REASON_SESSION, completed=False))
        return closed

    def close(self, visit_id: str, reason: str, completed: bool) -> Dict[str, Any]:
        """收口一次接待：补槽位、写统计、按需入队一条 `visit_report`。"""
        session = self.sessions.get(visit_id)
        if session is not None:
            session["closed"] = True
            session["reasons"].append(reason)

        visit = db.get_visit(self.conn, visit_id)
        if visit is None:
            return {"visit_id": visit_id, "closed": False, "error": "到访不存在"}

        person = db.get_person(self.conn, str(visit["face_id"]))
        rounds = self._user_turn_count(visit_id)
        purpose = visit["purpose"]
        caller = visit["caller_name"]
        summary = self._summary(person, purpose, caller, visit)

        db.finish_visit(
            self.conn, visit_id, purpose=purpose, caller_name=caller,
            summary=summary, rounds=rounds, completed=completed,
            silent_exit=(reason == REASON_SILENCE),
        )

        enqueued = False
        if purpose:
            # 只有"明确来意已成立"才入队汇总（四行矩阵第 1 行：只打招呼不入队）
            payload = self._report_payload(visit_id, completed)
            if payload is not None:
                enqueued = self.outbox.enqueue_report(visit_id, payload)
                if enqueued:
                    # 收口汇总也立刻试投一次；没人在线就留在队列等重连补发
                    try:
                        self.outbox.flush()
                    except Exception as exc:
                        self._log("[reception] 汇总即时投递失败：%s" % exc)

        db.log_event(self.conn, level="info", category="reception",
                     message="接待收口", detail={"visit_id": visit_id,
                                                  "reason": reason,
                                                  "completed": bool(completed),
                                                  "report_enqueued": bool(enqueued)})
        self._log("[reception] %s 收口（%s，completed=%s，report=%s）"
                  % (visit_id, reason, completed, enqueued))
        return {"visit_id": visit_id, "closed": True, "reason": reason,
                "completed": bool(completed), "report_enqueued": enqueued}

    # ---------------------------------------------------------------- 内部

    def _handle_privilege(self, visit, visit_id: str, text: str,
                          utterance_id: str, detection, source: str) -> Dict[str, Any]:
        """越权命中：先告警（立即），再回固定话术。不等收尾、不等照片。"""
        db.mark_privileged(self.conn, visit_id, detection.matched)
        # 越权原话单独落一条带 visit_id 的日志：汇总 payload 要带 escalation_phrases
        db.log_event(self.conn, level="warn", category="privilege",
                     message="越权请求", detail={"visit_id": visit_id,
                                                 "phrases": list(detection.matched),
                                                 "categories": list(detection.categories),
                                                 "utterance_id": utterance_id})
        reply = detection.reply
        key = "alert:%s:%s" % (visit_id, utterance_id)
        payload = {
            "type": "security_alert",
            "msg_id": key,
            "ts": self._now(),
            "visit_id": visit_id,
            "level": "high",
            "category": "privilege_escalation",
            "phrase": text,
            "reply_given": reply,
            "snapshot": None,
        }
        enqueued = self.outbox.enqueue_alert(visit_id, utterance_id, payload)
        # 复合句（"你开门，我送快递"）先告警、再保留来意：两者都要，互不替换
        intent = extract_intent(text)
        if intent["purpose"]:
            store.set_visit_slots(self.conn, visit_id, purpose=intent["purpose"])
        if intent["caller_name"]:
            store.set_visit_slots(self.conn, visit_id, caller_name=intent["caller_name"])
        if intent["urgent"]:
            store.set_visit_slots(self.conn, visit_id, urgent=True)
        store.add_turn(self.conn, visit_id, "assistant", reply, ts=self._now())
        # 安全关键：入队后立刻尝试投递一次，不等后台轮询（告警不等图片）
        try:
            self.outbox.flush()
        except Exception as exc:  # 投递失败不能把请求打挂，队列里有记录
            self._log("[reception] 告警即时投递失败：%s" % exc)
        return {"visit_id": visit_id, "reply": reply, "alert": True,
                "alert_key": key, "alert_enqueued": enqueued,
                "categories": list(detection.categories),
                "phrases": list(detection.matched),
                "farewell": False, "replayed": False, "source": source,
                "closed": False}

    def _replay_result(self, visit_id: str, turn_id: str) -> Dict[str, Any]:
        """重投处理：返回既有回复，任何计数都不许动。"""
        reply = ""
        if turn_id:
            row = self.conn.execute(
                "SELECT text FROM dialog_turn WHERE visit_id=? AND id > ?"
                " AND role='assistant' ORDER BY id ASC LIMIT 1",
                (visit_id, int(turn_id)),
            ).fetchone()
            if row is not None:
                reply = str(row["text"])
        self._log("[reception] 重复 utterance（visit=%s），返回既有结果" % visit_id)
        return {"visit_id": visit_id, "reply": reply, "alert": False,
                "replayed": True, "closed": False}

    def _reply_for(self, intent: Dict[str, Any], farewell: bool) -> str:
        """确定性回复（不接 LLM；第 4 阶段语音由天问51 播固定话术）。"""
        purpose = intent.get("purpose")
        caller = intent.get("caller_name")
        if farewell and purpose:
            return phrases.handoff(caller)
        if farewell:
            return "好的，我先记下您来过，再见。"
        if purpose:
            return "好的，已经帮您记下了，我会转告家里人的。"
        if caller:
            return "好的，%s，请问您来是有什么事？" % caller
        return "您好，家里现在没人，请问您找哪位？"

    def _user_turn_count(self, visit_id: str) -> int:
        row = self.conn.execute(
            "SELECT COUNT(*) AS n FROM dialog_turn WHERE visit_id=? AND role='user'",
            (visit_id,),
        ).fetchone()
        return int(row["n"]) if row is not None else 0

    def _summary(self, person, purpose: Optional[str],
                 caller: Optional[str], visit) -> Optional[str]:
        if not purpose:
            return None
        if caller:
            return "一位%s来%s" % (caller, purpose)
        if person is not None and str(person["name"] or "").strip():
            return "%s来%s" % (person["name"], purpose)
        return "一位访客来%s" % purpose

    def _report_payload(self, visit_id: str, completed: bool) -> Optional[Dict[str, Any]]:
        visit = db.get_visit(self.conn, visit_id)
        if visit is None:
            return None
        person = db.get_person(self.conn, str(visit["face_id"]))
        turns = []
        for row in db.turns_for(self.conn, visit_id):
            turns.append({"role": str(row["role"]), "text": str(row["text"]),
                          "ts": int(row["ts"])})
        rounds = sum(1 for turn in turns if turn["role"] == "user")
        key = "report:%s" % visit_id
        arrived = int(visit["arrived_at"])
        departed = visit["departed_at"]
        duration = visit["duration_ms"]
        return {
            "type": "visit_report",
            "msg_id": key,
            "ts": self._now(),
            "visit_id": visit_id,
            "person": {
                "face_id": str(visit["face_id"]),
                "is_known": bool(int(visit["is_known"])),
                "name": person["name"] if person is not None else None,
                "confidence": float(visit["confidence"] or 0.0),
                "visit_count": int(person["visit_count"]) if person is not None else 0,
                "first_seen": int(person["first_seen"]) if person is not None and person["first_seen"] is not None else None,
                "last_seen": int(person["last_seen"]) if person is not None and person["last_seen"] is not None else None,
            },
            "timing": {
                "arrived_at": arrived,
                "departed_at": int(departed) if departed is not None else None,
                "duration_ms": int(duration) if duration is not None else None,
            },
            "dialog": {
                "rounds": rounds,
                "turns": turns,
                "summary": visit["summary"],
                "purpose": visit["purpose"],
                "caller_name": visit["caller_name"],
                "urgent": bool(int(visit["urgent"] or 0)),
                "completed": bool(completed),
            },
            "security": {
                "privilege_escalation": bool(int(visit["privileged"] or 0)),
                "escalation_phrases": self._escalation_phrases(visit_id),
                "liveness_checked": False,
                "anomaly_sound": bool(int(visit["anomaly_sound"] or 0)),
                "silent_exit": bool(int(visit["silent_exit"] or 0)),
            },
            "snapshot": None,
            "home_state": str(visit["home_state"]),
        }

    def _escalation_phrases(self, visit_id: str) -> List[str]:
        """汇总里的"触发的具体越权语句"：从带 visit_id 的 privilege 日志回读。"""
        import json

        rows = self.conn.execute(
            "SELECT detail FROM event_log WHERE category='privilege'"
            " ORDER BY id ASC"
        ).fetchall()
        phrases: List[str] = []
        for row in rows:
            try:
                detail = json.loads(row["detail"] or "{}")
            except ValueError:
                continue
            if detail.get("visit_id") != visit_id:
                continue
            for phrase in detail.get("phrases") or []:
                if phrase not in phrases:
                    phrases.append(str(phrase))
        return phrases