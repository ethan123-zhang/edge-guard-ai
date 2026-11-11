"""HTTP 路由与鉴权。

单端口 8766 上：`Upgrade: websocket` 的走 `ws.py`，其余走这里。
约定（设计 §6）：
- 业务接口一律 `Bearer token`（或照片的 `?exp=&sig=` 签名，给外网推送用）；
- `/api/health` 不要求鉴权，方便探活；
- `/vision/snapshot` 用 `X-Vision-Key` 共享密钥，只允许局域网视觉节点调；
- 令牌未配置时**拒绝服务**（503），不静默放行。
"""
from __future__ import annotations

import json
import time
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import parse_qs

from dialog import db
from master import auth, snapshots, store
from master import config as master_config

JSON_LIMIT = 64 * 1024          # 请求体上限（只有 JSON 接口）
MIME = {
    ".html": "text/html; charset=utf-8",
    ".js": "application/javascript; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".json": "application/json; charset=utf-8",
    ".svg": "image/svg+xml",
    ".ico": "image/x-icon",
}


class Response(object):
    __slots__ = ("status", "headers", "body")

    def __init__(self, status: int, body: bytes = b"",
                 headers: Optional[Dict[str, str]] = None):
        self.status = int(status)
        self.body = body
        self.headers = headers or {}


def json_response(data: Any, status: int = 200) -> Response:
    body = json.dumps(data, ensure_ascii=False, sort_keys=True).encode("utf-8")
    return Response(status, body, {"Content-Type": "application/json; charset=utf-8"})


def error(status: int, message: str, code: Optional[str] = None) -> Response:
    payload = {"ok": False, "error": message}
    if code:
        payload["code"] = code
    return json_response(payload, status=status)


class Router(object):
    """把请求分到具体接口。所有 DB 访问由调用方持有 `app.lock`。"""

    def __init__(self, app):
        self.app = app

    # ---------------------------------------------------------------- 入口

    def handle(self, method: str, path: str, query: Dict[str, List[str]],
               headers: Dict[str, str], body: bytes, is_ws: bool = False) -> Response:
        app = self.app
        method = method.upper()

        if is_ws:
            return error(400, "WebSocket 请求应由服务端直接升级")

        if path.startswith("/api/"):
            return self._api(method, path, query, headers, body)
        if path == "/vision/snapshot":
            return self._vision_snapshot(method, query, headers, body)
        if method in ("GET", "HEAD"):
            return self._static(path)
        return error(405, "不支持的请求方法")

    # ---------------------------------------------------------------- 鉴权

    def _token(self, query: Dict[str, List[str]], headers: Dict[str, str]) -> Optional[str]:
        header = headers.get("authorization") or ""
        if header.lower().startswith("bearer "):
            return header[7:].strip()
        if query.get("token"):
            return str(query["token"][0])
        return None

    def _auth_device(self, query, headers):
        app = self.app
        salt = app.cfg["security"].get("token_salt", "")
        if not salt:
            return None, error(503, "服务端未配置 token_salt（环境变量 SENTINEL_TOKEN_SALT）",
                               code="not_configured")
        token = self._token(query, headers)
        row = auth.authenticate(app.conn, token, salt=salt)
        if row is None:
            return None, error(401, "未授权：设备 token 缺失或无效", code="unauthorized")
        return row, None

    # ---------------------------------------------------------------- API

    def _api(self, method, path, query, headers, body) -> Response:
        if path == "/api/health" and method == "GET":
            # 默认公开（局域网探活）；公网入口下由配置收紧，避免网址泄露即暴露"家里有没有人"
            if (self.app.cfg.get("security") or {}).get("health_requires_token"):
                device, fail = self._auth_device(query, headers)
                if fail:
                    return fail
            return self._health()
        if path == "/api/current" and method == "GET":
            device, fail = self._auth_device(query, headers)
            if fail:
                return fail
            return self._current()
        if path == "/api/visits" and method == "GET":
            device, fail = self._auth_device(query, headers)
            if fail:
                return fail
            return self._visits(query)
        if path == "/api/utterance" and method == "POST":
            device, fail = self._auth_device(query, headers)
            if fail:
                return fail
            return self._utterance(body)
        if path == "/api/ack" and method == "POST":
            device, fail = self._auth_device(query, headers)
            if fail:
                return fail
            return self._ack(body, device)

        # 带 id 的路径
        if path.startswith("/api/visits/"):
            rest = path[len("/api/visits/"):]
            if rest.endswith("/photo"):
                visit_id = rest[: -len("/photo")]
                return self._photo(visit_id, query, headers)
            if rest and "/" not in rest:
                device, fail = self._auth_device(query, headers)
                if fail:
                    return fail
                return self._visit_detail(rest)
        return error(404, "没有这个接口：%s" % path)

    # ---------------------------------------------------------------- 各接口

    def _health(self) -> Response:
        app = self.app
        now = int(time.time() * 1000)
        last_vision = int(app.last_vision_ts or 0)
        return json_response({
            "ok": True,
            "ts": now,
            "home_state": app.reception.home_state,
            "current_visit": app.reception_current_visit(),
            "pending_outbox": store.pending_count(app.conn),
            "clients": app.hub.count(),
            "vision_online": bool(last_vision and now - last_vision < 15000),
            "last_vision_ts": last_vision or None,
        })

    def _current(self) -> Response:
        app = self.app
        rows = app.conn.execute(
            "SELECT * FROM visit ORDER BY arrived_at DESC, created_at DESC LIMIT 5"
        ).fetchall()
        return json_response({
            "ok": True,
            "home_state": app.reception.home_state,
            "current_visit": app.reception_current_visit(),
            "recent": [self._visit_brief(row) for row in rows],
        })

    def _visits(self, query) -> Response:
        app = self.app
        try:
            limit = min(max(int((query.get("limit") or ["20"])[0]), 1), 100)
            before_ts = int((query.get("before_ts") or ["0"])[0] or 0)
        except ValueError:
            return error(400, "limit/before_ts 必须是整数")
        if before_ts > 0:
            rows = app.conn.execute(
                "SELECT * FROM visit WHERE arrived_at < ? ORDER BY arrived_at DESC LIMIT ?",
                (before_ts, limit),
            ).fetchall()
        else:
            rows = app.conn.execute(
                "SELECT * FROM visit ORDER BY arrived_at DESC LIMIT ?", (limit,)
            ).fetchall()
        items = [self._visit_brief(row) for row in rows]
        next_before = items[-1]["arrived_at"] if len(items) == limit else None
        return json_response({"ok": True, "visits": items, "next_before_ts": next_before})

    def _visit_detail(self, visit_id: str) -> Response:
        app = self.app
        visit = db.get_visit(app.conn, visit_id)
        if visit is None:
            return error(404, "到访不存在", code="not_found")
        person = db.get_person(app.conn, str(visit["face_id"]))
        turns = [{"role": str(row["role"]), "text": str(row["text"]),
                  "ts": int(row["ts"])}
                 for row in db.turns_for(app.conn, visit_id)]
        photo = snapshots.photo_for_visit(app.conn, visit_id)
        outbox = [{"message_key": str(row["message_key"]), "kind": str(row["kind"]),
                   "status": str(row["status"]), "attempts": int(row["attempts"]),
                   "last_error": row["last_error"]}
                  for row in store.outbox_rows(app.conn, visit_id)]
        escalation = self._escalation(visit_id)
        return json_response({
            "ok": True,
            "visit_id": visit_id,
            "arrived_at": int(visit["arrived_at"]),
            "departed_at": int(visit["departed_at"]) if visit["departed_at"] else None,
            "home_state": str(visit["home_state"]),
            "is_known": bool(int(visit["is_known"])),
            "name": person["name"] if person is not None else None,
            "visit_count": int(person["visit_count"]) if person is not None else 0,
            "face_id": str(visit["face_id"]),
            "confidence": float(visit["confidence"] or 0.0),
            "purpose": visit["purpose"],
            "caller_name": visit["caller_name"],
            "summary": visit["summary"],
            "completed": bool(int(visit["completed"] or 0)),
            "privileged": bool(int(visit["privileged"] or 0)),
            "silent_exit": bool(int(visit["silent_exit"] or 0)),
            "turns": turns,
            "escalation": escalation,
            "photo": ({"msg_id": photo["msg_id"], "ts": photo["frame_ts"]}
                      if photo else None),
            "photo_url": ("/api/visits/%s/photo" % visit_id) if photo else None,
            "outbox": outbox,
        })

    def _photo(self, visit_id: str, query, headers) -> Response:
        app = self.app
        visit = db.get_visit(app.conn, visit_id)
        if visit is None:
            return error(404, "到访不存在", code="not_found")
        photo = snapshots.photo_for_visit(app.conn, visit_id)
        if photo is None:
            return error(404, "这次到访没有照片（缺图不顶替）", code="no_photo")

        # 两种鉴权：Bearer token，或推送里带的短时签名
        authorized = False
        device, fail = self._auth_device(query, headers)
        if device is not None:
            authorized = True
        else:
            exp = (query.get("exp") or [""])[0]
            sig = (query.get("sig") or [""])[0]
            if exp and sig:
                secret = app.cfg["security"].get("photo_secret", "")
                authorized = auth.verify_photo_signature(
                    secret, visit_id, photo["msg_id"], int(exp) if exp.isdigit() else 0, sig)
        if not authorized:
            return error(401, "未授权：需要 Bearer token 或有效签名", code="unauthorized")

        path = snapshots.absolute_path(app.snapshots_root, photo["path"])
        root = app.snapshots_root.resolve()
        try:
            resolved = path.resolve()
            resolved.relative_to(root)
        except (ValueError, OSError):
            return error(403, "照片路径越界", code="forbidden")
        try:
            with open(str(resolved), "rb") as fh:
                data = fh.read()
        except OSError:
            return error(404, "照片文件缺失", code="no_photo")
        return Response(200, data, {"Content-Type": "image/jpeg",
                                    "Cache-Control": "private, max-age=60"})

    def _utterance(self, body: bytes) -> Response:
        app = self.app
        try:
            payload = json.loads(body.decode("utf-8") or "{}")
        except (ValueError, UnicodeDecodeError):
            return error(400, "请求体必须是 JSON")
        if not isinstance(payload, dict):
            return error(400, "请求体必须是 JSON 对象")
        visit_id = str(payload.get("visit_id") or "").strip()
        text = payload.get("text")
        utterance_id = str(payload.get("utterance_id") or "").strip()
        source = str(payload.get("source") or "keyboard")
        if not visit_id:
            return error(400, "visit_id 必填")
        try:
            result = app.reception.on_utterance(visit_id, text, utterance_id, source)
        except ValueError as exc:
            return error(400, str(exc))
        app.broadcast_state()
        return json_response({"ok": True, **result})

    def _ack(self, body: bytes, device) -> Response:
        app = self.app
        try:
            payload = json.loads(body.decode("utf-8") or "{}")
        except (ValueError, UnicodeDecodeError):
            return error(400, "请求体必须是 JSON")
        message_key = str((payload or {}).get("message_key") or "").strip()
        if not message_key:
            return error(400, "message_key 必填")
        app.outbox.note_client_shown(message_key, str(device["device_id"]))
        return json_response({"ok": True})

    # ---------------------------------------------------------------- 视觉侧

    def _vision_snapshot(self, method, query, headers, body) -> Response:
        if method != "POST":
            return error(405, "只支持 POST")
        app = self.app
        expected = app.cfg["security"].get("vision_key", "")
        if not expected:
            return error(503, "服务端未配置 vision_key", code="not_configured")
        if not auth.check_vision_key(headers.get("x-vision-key"), expected):
            return error(401, "X-Vision-Key 无效", code="unauthorized")
        msg_id = (query.get("msg_id") or [""])[0]
        face_id = (query.get("face_id") or [""])[0]
        frame_ts = (query.get("frame_ts") or ["0"])[0]
        if not msg_id or not face_id:
            return error(400, "msg_id/face_id 必填")
        try:
            info = snapshots.save(app.conn, app.snapshots_root, msg_id=msg_id,
                                  face_id=face_id, frame_ts=frame_ts, data=body,
                                  max_bytes=int(app.cfg["photo"]["max_bytes"]))
        except snapshots.SnapshotError as exc:
            return error(400, str(exc))
        app.last_vision_ts = int(time.time() * 1000)
        return json_response({"ok": True, **info})

    # ---------------------------------------------------------------- 静态页

    def _static(self, path: str) -> Response:
        app = self.app
        web_root = master_config.web_dir(app.cfg)
        rel = "index.html" if path in ("/", "") else path.lstrip("/")
        if ".." in rel or rel.startswith("/"):
            return error(403, "非法路径")
        target = web_root / rel
        try:
            target = target.resolve()
            target.relative_to(web_root.resolve())
        except (ValueError, OSError):
            return error(403, "非法路径")
        if not target.is_file():
            return error(404, "文件不存在")
        data = target.read_bytes()
        mime = MIME.get(target.suffix.lower(), "application/octet-stream")
        return Response(200, data, {"Content-Type": mime})

    # ---------------------------------------------------------------- 辅助

    def _visit_brief(self, row) -> Dict[str, Any]:
        app = self.app
        photo = snapshots.photo_for_visit(app.conn, str(row["visit_id"]))
        person = db.get_person(app.conn, str(row["face_id"]))
        return {
            "visit_id": str(row["visit_id"]),
            "arrived_at": int(row["arrived_at"]),
            "is_known": bool(int(row["is_known"])),
            "name": person["name"] if person is not None else None,
            "visit_count": int(person["visit_count"]) if person is not None else 0,
            "purpose": row["purpose"],
            "summary": row["summary"],
            "privileged": bool(int(row["privileged"] or 0)),
            "completed": bool(int(row["completed"] or 0)),
            "photo_url": ("/api/visits/%s/photo" % row["visit_id"]) if photo else None,
        }

    def _escalation(self, visit_id: str) -> List[str]:
        """详情页展示"越权原话"：从带 visit_id 的 privilege 日志读。"""
        app = self.app
        rows = app.conn.execute(
            "SELECT detail FROM event_log WHERE category='privilege' ORDER BY id ASC"
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