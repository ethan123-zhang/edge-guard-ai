"""主人端数据访问层：三张新表 + `dialog_turn` 的一列迁移。

设计约束（docs/主人端-设计与接口 §3）：
- **只加不删**：全部 `CREATE TABLE IF NOT EXISTS`，旧库启动即可用，不迁移数据。
- `outbox.message_key` 是幂等键：重试复用同一行、同一份冻结的 payload。
- 照片归属**只认来源事件 `msg_id`**，绝不按文件名/时间猜。

SQL 只在这里；业务判断在 `reception.py` 与 `outbox.py`。
"""
from __future__ import annotations

import json
import sqlite3
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

from dialog import db

Row = sqlite3.Row

#: 待发消息的状态取值（`failed` = 达到重试上限，停止自动重试，留给人工看）
STATUS_PENDING = "pending"
STATUS_SENT = "sent"
STATUS_FAILED = "failed"

#: 照片资源状态
SNAPSHOT_RECEIVED = "received"
SNAPSHOT_MISSING = "missing"
SNAPSHOT_FAILED = "failed"

SCHEMA = """
-- 待发消息：message_key 是幂等键，重试复用同一行、同一 payload
CREATE TABLE IF NOT EXISTS outbox (
    message_key     TEXT PRIMARY KEY,
    visit_id        TEXT NOT NULL,
    kind            TEXT NOT NULL,
    payload         TEXT NOT NULL,
    created_at      INTEGER NOT NULL,
    status          TEXT NOT NULL DEFAULT 'pending',
    attempts        INTEGER NOT NULL DEFAULT 0,
    last_attempt_at INTEGER,
    last_error      TEXT
);

CREATE INDEX IF NOT EXISTS idx_outbox_pending ON outbox(status) WHERE status != 'sent';

-- 照片资源：以来源事件 msg_id 为主键，绝不按文件名/时间猜归属
CREATE TABLE IF NOT EXISTS snapshot_asset (
    msg_id      TEXT PRIMARY KEY,
    face_id     TEXT NOT NULL,
    frame_ts    INTEGER NOT NULL,
    path        TEXT NOT NULL,
    bytes       INTEGER NOT NULL,
    status      TEXT NOT NULL,
    received_at INTEGER NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_snapshot_face ON snapshot_asset(face_id, frame_ts);

-- 主人端设备绑定：未登记/已吊销 token 一律拒绝；只存 sha256
CREATE TABLE IF NOT EXISTS client_device (
    device_id  TEXT PRIMARY KEY,
    name       TEXT,
    token_hash TEXT NOT NULL,
    created_at INTEGER NOT NULL,
    last_seen  INTEGER,
    revoked    INTEGER NOT NULL DEFAULT 0
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_device_token ON client_device(token_hash);
"""


def now_ms() -> int:
    return int(time.time() * 1000)


def init_schema(conn: sqlite3.Connection) -> None:
    """旧库升级入口：先确保旧表在（dialog.db 建），再加新表与列。"""
    db.init_schema(conn)
    conn.executescript(SCHEMA)
    _ensure_turn_utterance_column(conn)
    conn.commit()


def _ensure_turn_utterance_column(conn: sqlite3.Connection) -> None:
    """`dialog_turn` 只加一个可空列 `utterance_id`（接待入口幂等用）。

    旧行该列为 NULL；部分唯一索引用 `WHERE utterance_id IS NOT NULL` 避开它们。
    """
    columns = [row[1] for row in conn.execute("PRAGMA table_info(dialog_turn)")]
    if "utterance_id" not in columns:
        conn.execute("ALTER TABLE dialog_turn ADD COLUMN utterance_id TEXT")
    conn.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_turn_utterance"
        " ON dialog_turn(visit_id, utterance_id) WHERE utterance_id IS NOT NULL"
    )


# ---------------------------------------------------------------- outbox

def enqueue(
    conn: sqlite3.Connection,
    message_key: str,
    visit_id: str,
    kind: str,
    payload: Dict[str, Any],
    created_at: Optional[int] = None,
) -> bool:
    """入队一条待发消息。返回是否**新建**。

    已存在的 key 原样返回 False：payload 在创建时冻结，后来的输入只更新
    `visit` 行，不重建消息（任务书 §2.5 的幂等要求）。
    """
    ts = now_ms() if created_at is None else int(created_at)
    body = json.dumps(payload, ensure_ascii=False, sort_keys=True)
    try:
        conn.execute(
            "INSERT INTO outbox (message_key, visit_id, kind, payload, created_at,"
            " status, attempts) VALUES (?,?,?,?,?,?,0)",
            (message_key, visit_id, kind, body, ts, STATUS_PENDING),
        )
        conn.commit()
        return True
    except sqlite3.IntegrityError:
        conn.rollback()
        return False


def get_message(conn: sqlite3.Connection, message_key: str) -> Optional[Row]:
    return conn.execute(
        "SELECT * FROM outbox WHERE message_key=?", (message_key,)
    ).fetchone()


def payload_of(row: Row) -> Dict[str, Any]:
    return json.loads(row["payload"])


def pending_messages(
    conn: sqlite3.Connection,
    limit: int = 20,
    now: Optional[int] = None,
    retry_base_ms: int = 1000,
    retry_max_ms: int = 60000,
) -> List[Row]:
    """到点可重试的待发消息（指数退避，按创建时间先来先发）。"""
    ts = now_ms() if now is None else int(now)
    rows = conn.execute(
        "SELECT * FROM outbox WHERE status='pending' ORDER BY created_at ASC, message_key ASC"
    ).fetchall()
    ready = []
    for row in rows:
        attempts = int(row["attempts"] or 0)
        if attempts == 0:
            ready.append(row)
            continue
        delay = min(retry_base_ms * (2 ** min(attempts - 1, 16)), retry_max_ms)
        last = int(row["last_attempt_at"] or 0)
        if ts - last >= delay:
            ready.append(row)
        if len(ready) >= limit:
            break
    return ready[:limit]


def record_attempt(
    conn: sqlite3.Connection,
    message_key: str,
    ok: bool,
    error: Optional[str] = None,
    max_attempts: int = 100,
    now: Optional[int] = None,
) -> None:
    """记一次投递尝试。成功 → sent；连续失败到上限 → failed（停止自动重试）。"""
    ts = now_ms() if now is None else int(now)
    if ok:
        conn.execute(
            "UPDATE outbox SET status=?, attempts=attempts+1, last_attempt_at=?,"
            " last_error=NULL WHERE message_key=?",
            (STATUS_SENT, ts, message_key),
        )
    else:
        row = get_message(conn, message_key)
        attempts = int(row["attempts"] or 0) + 1 if row is not None else 1
        status = STATUS_FAILED if attempts >= max_attempts else STATUS_PENDING
        conn.execute(
            "UPDATE outbox SET status=?, attempts=attempts+1, last_attempt_at=?,"
            " last_error=? WHERE message_key=?",
            (status, ts, (error or "")[:500], message_key),
        )
    conn.commit()


def pending_count(conn: sqlite3.Connection) -> int:
    row = conn.execute(
        "SELECT COUNT(*) AS n FROM outbox WHERE status='pending'"
    ).fetchone()
    return int(row["n"]) if row is not None else 0


def outbox_rows(conn: sqlite3.Connection, visit_id: Optional[str] = None) -> List[Row]:
    if visit_id is None:
        return conn.execute("SELECT * FROM outbox ORDER BY created_at DESC").fetchall()
    return conn.execute(
        "SELECT * FROM outbox WHERE visit_id=? ORDER BY created_at DESC", (visit_id,)
    ).fetchall()


# ---------------------------------------------------------------- snapshot_asset

def record_snapshot(
    conn: sqlite3.Connection,
    msg_id: str,
    face_id: str,
    frame_ts: int,
    path: str,
    nbytes: int,
    status: str = SNAPSHOT_RECEIVED,
    received_at: Optional[int] = None,
) -> None:
    """登记一张照片。同一 `msg_id` 重复上传只保留第一次（图片不覆盖、不重复计）。"""
    ts = now_ms() if received_at is None else int(received_at)
    try:
        conn.execute(
            "INSERT INTO snapshot_asset (msg_id, face_id, frame_ts, path, bytes,"
            " status, received_at) VALUES (?,?,?,?,?,?,?)",
            (msg_id, face_id, int(frame_ts), path, int(nbytes), status, ts),
        )
        conn.commit()
    except sqlite3.IntegrityError:
        conn.rollback()


def get_snapshot(conn: sqlite3.Connection, msg_id: str) -> Optional[Row]:
    return conn.execute(
        "SELECT * FROM snapshot_asset WHERE msg_id=?", (msg_id,)
    ).fetchone()


def photo_for_visit(conn: sqlite3.Connection, visit_id: str) -> Optional[Row]:
    """该次到访"最早一条已收到"的照片（确定性选图，见设计 §5）。"""
    return conn.execute(
        "SELECT sa.* FROM snapshot_asset sa"
        " JOIN visit_event ve ON ve.msg_id = sa.msg_id"
        " WHERE ve.visit_id = ? AND sa.status = 'received'"
        " ORDER BY sa.frame_ts ASC, sa.msg_id ASC LIMIT 1",
        (visit_id,),
    ).fetchone()


# ---------------------------------------------------------------- client_device

def upsert_device(
    conn: sqlite3.Connection,
    device_id: str,
    name: str,
    token_hash: str,
    created_at: Optional[int] = None,
) -> None:
    ts = now_ms() if created_at is None else int(created_at)
    conn.execute(
        "INSERT INTO client_device (device_id, name, token_hash, created_at, revoked)"
        " VALUES (?,?,?,?,0)"
        " ON CONFLICT(device_id) DO UPDATE SET name=excluded.name,"
        " token_hash=excluded.token_hash, revoked=0",
        (device_id, name, token_hash, ts),
    )
    conn.commit()


def device_by_token_hash(conn: sqlite3.Connection, token_hash: str) -> Optional[Row]:
    return conn.execute(
        "SELECT * FROM client_device WHERE token_hash=? AND revoked=0", (token_hash,)
    ).fetchone()


def get_device(conn: sqlite3.Connection, device_id: str) -> Optional[Row]:
    return conn.execute(
        "SELECT * FROM client_device WHERE device_id=?", (device_id,)
    ).fetchone()


def touch_device(conn: sqlite3.Connection, device_id: str, ts: Optional[int] = None) -> None:
    conn.execute(
        "UPDATE client_device SET last_seen=? WHERE device_id=?",
        (now_ms() if ts is None else int(ts), device_id),
    )
    conn.commit()


def revoke_device(conn: sqlite3.Connection, device_id: str) -> None:
    conn.execute("UPDATE client_device SET revoked=1 WHERE device_id=?", (device_id,))
    conn.commit()


# ---------------------------------------------------------------- dialog_turn 幂等

def find_turn(conn: sqlite3.Connection, visit_id: str, utterance_id: str) -> Optional[Row]:
    return conn.execute(
        "SELECT * FROM dialog_turn WHERE visit_id=? AND utterance_id=?",
        (visit_id, utterance_id),
    ).fetchone()


def add_turn(
    conn: sqlite3.Connection,
    visit_id: str,
    role: str,
    text: str,
    utterance_id: Optional[str] = None,
    ts: Optional[int] = None,
) -> bool:
    """写一条对话。返回 False 表示 `(visit_id, utterance_id)` 已存在（幂等重投）。

    幂等键落在 `dialog_turn` 上（设计 §3 迁移说明），**不是**在内存里记：
    服务重启后重放同一条 utterance 仍然只算一次。
    """
    ts_value = now_ms() if ts is None else int(ts)
    try:
        conn.execute(
            "INSERT INTO dialog_turn (visit_id, role, text, ts, utterance_id)"
            " VALUES (?,?,?,?,?)",
            (visit_id, role, text, ts_value, utterance_id),
        )
        conn.commit()
        return True
    except sqlite3.IntegrityError:
        conn.rollback()
        return False


# ---------------------------------------------------------------- visit 槽位

def set_visit_slots(
    conn: sqlite3.Connection,
    visit_id: str,
    purpose: Optional[str] = None,
    caller_name: Optional[str] = None,
    summary: Optional[str] = None,
    rounds: Optional[int] = None,
    urgent: Optional[bool] = None,
) -> None:
    """接待过程中增量更新槽位。只写传入的字段（None = 不动）。"""
    sets: List[str] = []
    values: List[Any] = []
    for column, value in (("purpose", purpose), ("caller_name", caller_name),
                          ("summary", summary), ("dialog_rounds", rounds),
                          ("urgent", None if urgent is None else (1 if urgent else 0))):
        if value is not None:
            sets.append("%s=?" % column)
            values.append(value)
    if not sets:
        return
    values.append(visit_id)
    conn.execute("UPDATE visit SET %s WHERE visit_id=?" % ", ".join(sets), values)
    conn.commit()