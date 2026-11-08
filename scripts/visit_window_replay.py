"""回放到访窗口：把 face_event 序列喂给 `dialog.visits.VisitBook`，输出归并结果。

用途：
1. **现场夹具回放**：`logs/video-validation/natural-head-full.json` 里 12 条真实链路的
   face_event（全是 `unk_*`），用它证明业务层不会替视觉层"猜"不同匿名 ID 是同一个人；
2. **合成已知人脸时间线**：把 599999/600000 边界、活跃长停留、msg_id 重传、乱序送达
   这些规则跑成一次可复核的断言，产出 `docs/data/visit-window-<日期>.json` 证据。

只读夹具、只写临时 SQLite；不改生产库、不联网。

用法：
    py -3.12 scripts/visit_window_replay.py
    py -3.12 scripts/visit_window_replay.py --fixture <path> --out <json>
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
import time
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

from dialog import db, visits                      # noqa: E402
from vision import events as vision_events         # noqa: E402

BASE = 1735689600000                               # 固定毫秒基准（2025-01-01T00:00:00Z）
MIN = 60 * 1000


def fresh_db():
    tmpdir = tempfile.TemporaryDirectory()
    conn = db.connect(Path(tmpdir.name) / "sentinel.db")
    db.init_schema(conn)
    return tmpdir, conn


def replay_natural(fixture_path, window_ms):
    """真实夹具回放：12 条 unk 事件 → 应当各建各的到访（业务层不猜）。"""
    with open(str(fixture_path), "r", encoding="utf-8") as fh:
        data = json.load(fh)
    raw = data.get("events") or []
    messages = []
    problems = []
    for item in raw:
        msg = item.get("face_event") if isinstance(item, dict) else None
        if msg is None:
            continue
        found = vision_events.check(msg)
        if found:
            problems.append({"msg_id": msg.get("msg_id"), "problems": found})
        messages.append(msg)

    tmpdir, conn = fresh_db()
    try:
        book = visits.VisitBook(conn, window_ms=window_ms)
        rows = []
        for msg in messages:
            decision = book.arrive(msg)
            rows.append({
                "frame_ts": int(msg["frame_ts"]),
                "face_id": msg["face_id"],
                "visit_id": decision.visit_id,
                "created": decision.created,
                "reason": decision.reason,
            })
        visit_count = conn.execute("SELECT COUNT(*) FROM visit").fetchone()[0]
        reuse = sum(1 for row in rows if not row["created"])
        face_ids = set(row["face_id"] for row in rows)
    finally:
        conn.close()
        tmpdir.cleanup()

    return {
        "path": _rel(fixture_path),
        "event_count": len(messages),
        "protocol_violations": len(problems),
        "violations": problems,
        "distinct_face_ids": len(face_ids),
        "visits_created": int(visit_count),
        "reused": reuse,
        "per_event": rows,
        "verdict": ("业务层不跨 unk_ID 归并：%d 条事件 → %d 次到访（reused=%d）"
                    % (len(messages), visit_count, reuse)),
    }


def replay_known(window_ms):
    """合成已知人脸时间线：规则断言 + 证据。

    时间线（同一人 p_10023，t=0 为首次到达）：
      0        新建到访 v1
      5 分钟    收口后仍在窗口内 → 并入 v1
      9 分钟    同上 → 并入 v1
      10 分钟   恰好窗口边界（600000ms）→ 新建 v2
      25 分钟   v2 仍活跃，超窗口也并入（活跃长停留不拆）
      重传      最早的 msg_id 再来一次 → 忽略、次数不动
    """
    tmpdir, conn = fresh_db()
    steps = []
    checks = []
    try:
        book = visits.VisitBook(conn, window_ms=window_ms)
        first_msg = _event("p_10023", BASE, "m-known-1", is_known=True, name="王阿姨")

        d1 = book.arrive(first_msg)
        steps.append(_step("到达", BASE, d1))

        for offset, msg_id in ((5 * MIN, "m-known-2"), (9 * MIN, "m-known-3")):
            decision = book.arrive(_event("p_10023", BASE + offset, msg_id,
                                          is_known=True, name="王阿姨"))
            steps.append(_step("窗口内再出现", BASE + offset, decision))
            db.finish_visit(conn, decision.visit_id,
                            departed_at=BASE + offset + 60 * 1000, completed=True)

        d_boundary = book.arrive(_event("p_10023", BASE + 10 * MIN, "m-known-4",
                                        is_known=True, name="王阿姨"))
        steps.append(_step("恰好 600000ms", BASE + 10 * MIN, d_boundary))

        d_long = book.arrive(_event("p_10023", BASE + 25 * MIN, "m-known-5",
                                    is_known=True, name="王阿姨"))
        steps.append(_step("活跃长停留（+15min）", BASE + 25 * MIN, d_long))

        replay = book.arrive(_event("p_10023", BASE, "m-known-1",
                                    is_known=True, name="王阿姨"))
        steps.append(_step("重传第一条 msg_id", BASE, replay))

        checks = [
            _check("0ms 新建", d1.created and d1.person_visit_count == 1),
            _check("5min 并入同一次", steps[1]["created"] is False),
            _check("9min 并入同一次", steps[2]["created"] is False),
            _check("600000ms 另建（含本次计数 2）",
                   d_boundary.created and d_boundary.person_visit_count == 2),
            _check("活跃长停留不拆", (not d_long.created)
                   and d_long.visit_id == d_boundary.visit_id),
            _check("重传不重复计数", (not replay.created)
                   and replay.reason == visits.REASON_REPLAY
                   and replay.person_visit_count == 2),
            _check("到访总数 = 2", _count(conn, "visit") == 2),
        ]
        person = db.get_person(conn, "p_10023")
        counts = {"person_visit_count": int(person["visit_count"]),
                  "visit_rows": _count(conn, "visit")}
    finally:
        conn.close()
        tmpdir.cleanup()

    return {
        "person": "p_10023 王阿姨",
        "timeline": steps,
        "checks": checks,
        "counts": counts,
        "verdict": ("%d 项检查%s" % (len(checks),
                                     "全过" if all(c["ok"] for c in checks) else "有失败")),
    }


def replay_unknown_pair(window_ms):
    """两个不同匿名 ID 在同一秒先后出现：必须各建一次到访（不按 bbox/时间合并）。"""
    tmpdir, conn = fresh_db()
    try:
        book = visits.VisitBook(conn, window_ms=window_ms)
        a = book.arrive(_event("unk_1", BASE, "m-u1", is_known=False, name=None,
                               confidence=0.0))
        b = book.arrive(_event("unk_2", BASE + 1000, "m-u2", is_known=False,
                               name=None, confidence=0.0))
        same = book.arrive(_event("unk_1", BASE + 2000, "m-u3", is_known=False,
                                  name=None, confidence=0.0))
        visits_total = _count(conn, "visit")
    finally:
        conn.close()
        tmpdir.cleanup()
    checks = [
        _check("unk_1 新建", a.created),
        _check("unk_2 各建各的", b.created and b.visit_id != a.visit_id),
        _check("unk_1 再出现并入自己", (not same.created)
               and same.visit_id == a.visit_id),
        _check("到访总数 = 2", visits_total == 2),
    ]
    return {"checks": checks, "visits": visits_total,
            "verdict": "%d 项检查%s" % (len(checks),
                                        "全过" if all(c["ok"] for c in checks) else "有失败")}


def _event(face_id, frame_ts, msg_id, is_known, name, confidence=0.9):
    msg = vision_events.face_event(face_id=face_id, is_known=is_known, name=name,
                                   confidence=confidence, frame_ts=frame_ts,
                                   bbox=(100, 80, 80, 80), ts=frame_ts)
    # face_event() 每次都生成新 msg_id；回放要能指定"同一条消息重传"
    msg["msg_id"] = msg_id
    return msg


def _step(label, frame_ts, decision):
    return {"label": label, "frame_ts": frame_ts, "visit_id": decision.visit_id,
            "created": decision.created, "reason": decision.reason,
            "person_visit_count": decision.person_visit_count}


def _check(label, ok):
    return {"label": label, "ok": bool(ok)}


def _count(conn, table):
    return int(conn.execute("SELECT COUNT(*) FROM %s" % table).fetchone()[0])


def _rel(path):
    try:
        return os.path.relpath(str(path), str(REPO)).replace("\\", "/")
    except ValueError:
        return str(path)


def main(argv=None):
    parser = argparse.ArgumentParser(description="回放 face_event → 到访窗口归并")
    parser.add_argument("--fixture", default=str(REPO / "logs" / "video-validation"
                                                 / "natural-head-full.json"))
    parser.add_argument("--out", default=str(REPO / "docs" / "data"
                                             / ("visit-window-%s.json"
                                                % time.strftime("%Y-%m-%d"))))
    parser.add_argument("--window-min", type=float, default=visits.WINDOW_MIN)
    args = parser.parse_args(argv)

    window_ms = int(round(args.window_min * 60 * 1000))
    report = {
        "schema": "sentinel-manager/visit-window-replay@1",
        "generated_at_local": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "window_ms": window_ms,
        "window_min": args.window_min,
        "python": sys.version.split()[0],
    }

    ok = True
    if os.path.exists(args.fixture):
        report["natural_fixture"] = replay_natural(args.fixture, window_ms)
        print("[fixture] %s" % report["natural_fixture"]["verdict"])
    else:
        report["natural_fixture"] = {"path": _rel(args.fixture), "skipped": "夹具不在本机",
                                     "verdict": "跳过"}
        print("[fixture] 跳过：%s 不存在" % args.fixture)

    report["known_synthetic"] = replay_known(window_ms)
    report["unknown_pair"] = replay_unknown_pair(window_ms)
    for section in ("known_synthetic", "unknown_pair"):
        print("[%s] %s" % (section, report[section]["verdict"]))
        for item in report[section]["checks"]:
            print("   %s %s" % ("OK  " if item["ok"] else "FAIL", item["label"]))
            ok = ok and item["ok"]

    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    with open(str(out), "w", encoding="utf-8") as fh:
        json.dump(report, fh, ensure_ascii=False, indent=2)
        fh.write("\n")
    print("[out] %s" % _rel(out))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())