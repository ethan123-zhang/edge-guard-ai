"""数据访问层（docs/02 §6 表结构）。

两个不能出错的地方：
1. **断网补传**：`visit.report_status` 从 `pending` → `sent`，启动时扫非 sent 重试（核心亮点 ③）
2. **熟人记忆**：`person.visit_count` / `first_seen` 是"第 N 次来"的唯一真源（核心亮点 ①）

只做 SQL，不含业务判断 —— 状态机在 `state_machine.py`。
"""
from __future__ import annotations

import sqlite3
import time
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional

Row = sqlite3.Row

SCHEMA = """
CREATE TABLE IF NOT EXISTS person (
    face_id       TEXT PRIMARY KEY,
    name          TEXT,
    is_known      INTEGER NOT NULL DEFAULT 0,
    visit_count   INTEGER NOT NULL DEFAULT 0,
    first_seen    INTEGER,
    last_seen     INTEGER,
    note          TEXT
);

CREATE TABLE IF NOT EXISTS visit (
    visit_id      TEXT PRIMARY KEY,
    face_id       TEXT NOT NULL,
    arrived_at    INTEGER NOT NULL,
    departed_at   INTEGER,
    duration_ms   INTEGER,
    home_state    TEXT NOT NULL,
    is_known      INTEGER NOT NULL,
    confidence    REAL,
    purpose       TEXT,
    caller_name   TEXT,
    urgent        INTEGER DEFAULT 0,
    summary       TEXT,
    dialog_rounds INTEGER DEFAULT 0,
    completed     INTEGER DEFAULT 0,
    privileged    INTEGER DEFAULT 0,
    anomaly_sound INTEGER DEFAULT 0,
    silent_exit   INTEGER DEFAULT 0,
    snapshot      TEXT,
    report_status TEXT NOT NULL DEFAULT 'pending',
    created_at    INTEGER NOT NULL,
    FOREIGN KEY (face_id) REFERENCES person(face_id)
);

CREATE INDEX IF NOT EXISTS idx_visit_face    ON visit(face_id);
CREATE INDEX IF NOT EXISTS idx_visit_arrived ON visit(arrived_at DESC);
CREATE INDEX IF NOT EXISTS idx_visit_pending ON visit(report_status)
    WHERE report_status != 'sent';

CREATE TABLE IF NOT EXISTS dialog_turn (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    visit_id   TEXT NOT NULL,
    role       TEXT NOT NULL,
    text       TEXT NOT NULL,
    ts         INTEGER NOT NULL,
    FOREIGN KEY (visit_id) REFERENCES visit(visit_id)
);

CREATE INDEX IF NOT EXISTS idx_turn_visit ON dialog_turn(visit_id, ts);

CREATE TABLE IF NOT EXISTS event_log (
    id       INTEGER PRIMARY KEY AUTOINCREMENT,
    ts       INTEGER NOT NULL,
    level    TEXT NOT NULL,
    category TEXT NOT NULL,
    message  TEXT NOT NULL,
    detail   TEXT
);

CREATE INDEX IF NOT EXISTS idx_log_ts ON event_log(ts DESC);

CREATE TABLE IF NOT EXISTS face_registry (
    face_id      TEXT PRIMARY KEY,
    name         TEXT NOT NULL,
    feature_blob BLOB,
    feature_path TEXT,
    created_at   INTEGER NOT NULL
);
"""


def now_ms() -> int:
    return int(time.time() * 1000)


def connect(db_path) -> sqlite3.Connection:
    """打开连接。行按列名访问（`row["name"]`），避免下标错位。

    注意：返回的连接带 `check_same_thread=False`，因为 ASR/TTS 各起线程。
    写操作由调用方串行化（见 state_machine）。
    """
    path = Path(db_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path), check_same_thread=False)
    conn.row_factory = Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def init_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA)
    conn.commit()


# ---------------------------------------------------------------- person

def upsert_person(
    conn: sqlite3.Connection,
    face_id: str,
    is_known: bool,
    now_ms: int,
    name: Optional[str] = None,
    note: Optional[str] = None,
) -> Row:
    """来人登场时调用。首次创建；重复则更新 last_seen 与 visit_count。

    `visit_count` 语义是「**已完成的**到访次数」，所以这里在
    `create_visit` 之前调用，累加由 `count_visit` 做。
    """
    row = conn.execute("SELECT * FROM person WHERE face_id=?", (face_id,)).fetchone()
    if row is None:
        conn.execute(
            "INSERT INTO person (face_id, name, is_known, visit_count, first_seen,"
            " last_seen, note) VALUES (?,?,?,0,?,?,?)",
            (face_id, name, 1 if is_known else 0, now_ms, now_ms, note),
        )
        conn.commit()
        return conn.execute("SELECT * FROM person WHERE face_id=?", (face_id,)).fetchone()

    conn.execute(
        "UPDATE person SET last_seen=?,"
        " name=COALESCE(?, name), is_known=?, note=COALESCE(?, note)"
        " WHERE face_id=?",
        (now_ms, name, 1 if is_known else 0, note, face_id),
    )
    conn.commit()
    return conn.execute("SELECT * FROM person WHERE face_id=?", (face_id,)).fetchone()


def count_visit(conn: sqlite3.Connection, face_id: str) -> int:
    """累加到访次数，返回**含本次**的累计值（docs/02 §4.1 字段说明）。"""
    conn.execute(
        "UPDATE person SET visit_count = visit_count + 1 WHERE face_id=?", (face_id,)
    )
    conn.commit()
    row = conn.execute(
        "SELECT visit_count FROM person WHERE face_id=?", (face_id,)
    ).fetchone()
    return int(row["visit_count"]) if row else 0


def get_person(conn: sqlite3.Connection, face_id: str) -> Optional[Row]:
    return conn.execute("SELECT * FROM person WHERE face_id=?", (face_id,)).fetchone()


# ---------------------------------------------------------------- visit

def _next_sequence(conn: sqlite3.Connection, prefix: str) -> int:
    row = conn.execute(
        "SELECT visit_id FROM visit WHERE visit_id LIKE ? ORDER BY visit_id DESC LIMIT 1",
        (prefix + "%",),
    ).fetchone()
    if row is None:
        return 1
    return int(str(row["visit_id"]).rsplit("_", 1)[-1]) + 1


def create_visit(
    conn: sqlite3.Connection,
    face_id: str,
    arrived_at: int,
    home_state: str,
    is_known: bool,
    confidence: Optional[float] = None,
    now_ms: Optional[int] = None,
) -> str:
    """建一条到访记录，返回 `v_YYYYMMDD_NNNN`。初始 `report_status='pending'`。"""
    ts = now_ms if now_ms is not None else arrived_at
    day = time.strftime("%Y%m%d", time.localtime(arrived_at / 1000))
    prefix = f"v_{day}_"
    seq = _next_sequence(conn, prefix)
    visit_id = f"{prefix}{seq:04d}"
    conn.execute(
        "INSERT INTO visit (visit_id, face_id, arrived_at, home_state, is_known,"
        " confidence, report_status, created_at) VALUES (?,?,?,?,?,?,'pending',?)",
        (visit_id, face_id, arrived_at, home_state, 1 if is_known else 0, confidence, ts),
    )
    conn.commit()
    return visit_id


def get_visit(conn: sqlite3.Connection, visit_id: str) -> Optional[Row]:
    return conn.execute("SELECT * FROM visit WHERE visit_id=?", (visit_id,)).fetchone()


def finish_visit(
    conn: sqlite3.Connection,
    visit_id: str,
    purpose: Optional[str] = None,
    caller_name: Optional[str] = None,
    summary: Optional[str] = None,
    rounds: int = 0,
    completed: bool = False,
    departed_at: Optional[int] = None,
    urgent: bool = False,
    silent_exit: bool = False,
) -> None:
    """对话结束时收口，补齐槽位与统计字段。"""
    duration = None
    if departed_at is not None:
        row = get_visit(conn, visit_id)
        if row is not None:
            duration = departed_at - int(row["arrived_at"])
    conn.execute(
        "UPDATE visit SET purpose=?, caller_name=?, summary=?, dialog_rounds=?,"
        " completed=?, departed_at=COALESCE(?, departed_at),"
        " duration_ms=COALESCE(?, duration_ms), urgent=?, silent_exit=?"
        " WHERE visit_id=?",
        (
            purpose, caller_name, summary, rounds, 1 if completed else 0,
            departed_at, duration, 1 if urgent else 0, 1 if silent_exit else 0,
            visit_id,
        ),
    )
    conn.commit()


def mark_privileged(
    conn: sqlite3.Connection, visit_id: str, phrases: Iterable[str] = ()
) -> None:
    """记一次越权命中。同时落 event_log，便于事后统计拦截率。"""
    conn.execute("UPDATE visit SET privileged=1 WHERE visit_id=?", (visit_id,))
    conn.commit()
    text = "、".join(phrases)
    log_event(conn, level="warn", category="dialog",
              message="越权请求被拦截", detail={"phrases": text})


def pending_reports(conn: sqlite3.Connection, limit: int = 50) -> List[Row]:
    """断网补传用：所有还没推成功的记录（核心亮点 ③）。"""
    return conn.execute(
        "SELECT * FROM visit WHERE report_status != 'sent'"
        " ORDER BY arrived_at ASC LIMIT ?",
        (limit,),
    ).fetchall()


def mark_report_sent(conn: sqlite3.Connection, visit_id: str) -> None:
    conn.execute(
        "UPDATE visit SET report_status='sent' WHERE visit_id=?", (visit_id,)
    )
    conn.commit()


def mark_report_failed(conn: sqlite3.Connection, visit_id: str) -> None:
    conn.execute(
        "UPDATE visit SET report_status='failed' WHERE visit_id=?", (visit_id,)
    )
    conn.commit()


# ---------------------------------------------------------------- dialog_turn

def add_turn(
    conn: sqlite3.Connection, visit_id: str, role: str, text: str,
    ts: Optional[int] = None,
) -> None:
    conn.execute(
        "INSERT INTO dialog_turn (visit_id, role, text, ts) VALUES (?,?,?,?)",
        (visit_id, role, text, ts if ts is not None else now_ms()),
    )
    conn.commit()


def turns_for(conn: sqlite3.Connection, visit_id: str) -> List[Row]:
    return conn.execute(
        "SELECT * FROM dialog_turn WHERE visit_id=? ORDER BY ts ASC, id ASC",
        (visit_id,),
    ).fetchall()


# ---------------------------------------------------------------- event_log

def log_event(
    conn: sqlite3.Connection,
    level: str,
    category: str,
    message: str,
    detail: Optional[Dict[str, Any]] = None,
) -> None:
    import json

    conn.execute(
        "INSERT INTO event_log (ts, level, category, message, detail)"
        " VALUES (?,?,?,?,?)",
        (now_ms(), level, category, message,
         json.dumps(detail, ensure_ascii=False) if detail else None),
    )
    conn.commit()


def recent_events(conn: sqlite3.Connection, limit: int = 20) -> List[Row]:
    return conn.execute(
        "SELECT * FROM event_log ORDER BY ts DESC, id DESC LIMIT ?", (limit,)
    ).fetchall()
