"""待发消息队列：稳定键 + 退避重试 + 三种观测点。

为什么必须有这一层（任务书 §2.5 / §阶段3 陷阱）：
- `visit.report_status` 不是补传真源 —— 只留图的到访会被置成 `local_only`，
  旧逻辑扫 `report_status != 'sent'` 会把它们当"没送达"反复补发。所以**只扫 outbox**。
- 消息键 `report:<visit_id>` / `alert:<visit_id>:<utterance_id>` 让重试复用同一行，
  客户端按 key 去重，不会收到两条相同的汇总。
- 三种观测点分别落 `event_log`：`push_accept`（推送平台受理）、`lan_ack`（接入方确认）、
  `client_shown`（手机展示）。三者不是一回事，不许合并。
"""
from __future__ import annotations

import time
from typing import Any, Callable, Dict, Optional, Tuple

from dialog import db
from master import config as master_config
from master import store

REPORT_KIND = "visit_report"
ALERT_KIND = "security_alert"

OBS_PUSH_ACCEPT = "push_accept"
OBS_LAN_ACK = "lan_ack"
OBS_CLIENT_SHOWN = "client_shown"


def report_key(visit_id: str) -> str:
    return "report:%s" % visit_id


def alert_key(visit_id: str, utterance_id: str) -> str:
    return "alert:%s:%s" % (visit_id, utterance_id)


class Outbox(object):
    """入队 / 发送 / 收敛。线程安全由调用方（server 的全局锁）保证。"""

    def __init__(self, conn, adapter, cfg: Dict[str, Any],
                 now_fn: Optional[Callable[[], int]] = None,
                 log: Optional[Callable[[str], None]] = None):
        self.conn = conn
        self.adapter = adapter
        self.cfg = cfg
        self._now = now_fn or (lambda: int(time.time() * 1000))
        self._log = log or (lambda message: None)
        outbox_cfg = cfg.get("outbox") or {}
        self.retry_base_ms = int(outbox_cfg.get("retry_base_ms", 1000))
        self.retry_max_ms = int(outbox_cfg.get("retry_max_ms", 60000))
        self.batch = int(outbox_cfg.get("batch", 20))

    # ---------------------------------------------------------------- 入队

    def enqueue(self, message_key: str, visit_id: str, kind: str,
                payload: Dict[str, Any]) -> bool:
        created = store.enqueue(self.conn, message_key, visit_id, kind, payload,
                                created_at=self._now())
        if created:
            db.log_event(self.conn, level="info", category="outbox",
                         message="消息入队", detail={"key": message_key, "kind": kind})
            self._log("[outbox] 入队 %s（%s）" % (message_key, kind))
        else:
            self._log("[outbox] 已存在 %s，保留原 payload（幂等）" % message_key)
        return created

    def enqueue_report(self, visit_id: str, payload: Dict[str, Any]) -> bool:
        return self.enqueue(report_key(visit_id), visit_id, REPORT_KIND, payload)

    def enqueue_alert(self, visit_id: str, utterance_id: str,
                      payload: Dict[str, Any]) -> bool:
        return self.enqueue(alert_key(visit_id, utterance_id), visit_id,
                            ALERT_KIND, payload)

    # ---------------------------------------------------------------- 发送

    def flush(self) -> int:
        """把到点的待发消息投出去，返回本轮成功条数。"""
        rows = store.pending_messages(
            self.conn, limit=self.batch, now=self._now(),
            retry_base_ms=self.retry_base_ms, retry_max_ms=self.retry_max_ms,
        )
        sent = 0
        for row in rows:
            key = str(row["message_key"])
            payload = store.payload_of(row)
            try:
                ok, error = self.adapter.send(key, payload)
            except Exception as exc:  # 适配器异常不能把队列卡死
                ok, error = False, "%s: %s" % (type(exc).__name__, exc)
            if ok:
                store.record_attempt(self.conn, key, ok=True, now=self._now())
                sent += 1
                db.log_event(self.conn, level="info", category=OBS_PUSH_ACCEPT,
                             message="消息已受理", detail={"key": key})
                self._log("[outbox] 已发 %s" % key)
            else:
                store.record_attempt(self.conn, key, ok=False, error=error,
                                     now=self._now())
                self._log("[outbox] 未发 %s：%s" % (key, error))
        return sent

    def pending_count(self) -> int:
        return store.pending_count(self.conn)

    # ---------------------------------------------------------------- 观测点

    def note_lan_ack(self, message_key: str, device_id: Optional[str] = None) -> None:
        """接入方（手机/网页）回 ack —— 与"推送平台受理"是两个观测点。"""
        db.log_event(self.conn, level="info", category=OBS_LAN_ACK,
                     message="接入方确认收到",
                     detail={"key": message_key, "device_id": device_id})

    def note_client_shown(self, message_key: str, device_id: Optional[str] = None) -> None:
        db.log_event(self.conn, level="info", category=OBS_CLIENT_SHOWN,
                     message="手机已展示",
                     detail={"key": message_key, "device_id": device_id})