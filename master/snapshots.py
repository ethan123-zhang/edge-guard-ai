"""照片链路：JPEG 原子落盘 + 按来源事件 `msg_id` 登记。

不能出错的地方（设计 §5）：
- **只认 `msg_id`**：照片归属由产生它的事件决定，不看文件名、不看修改时间、
  不看"当前屏幕上是哪个人"（任务书 §二.4）。
- **原子写**：先写 `.tmp` 再 `os.replace()`，手机/看板不会读到半张图。
- **缺图不顶替**：没有照片就是没有，返回 404，调用方显示"缺图·采集中"。

`msg_id` 会被拼进文件路径，所以这里做白名单校验 —— 不允许 `..`、斜杠、控制字符。
"""
from __future__ import annotations

import os
import re
import time
from pathlib import Path
from typing import Any, Dict, Optional

from master import store

_ID_RE = re.compile(r"^[A-Za-z0-9_.:-]{1,128}$")
_DAY_FMT = "%Y%m%d"


class SnapshotError(ValueError):
    """参数非法（不是服务器故障，调用方转 400）。"""


def _check_id(value: str, field: str) -> str:
    if not isinstance(value, str) or not _ID_RE.match(value) or value.startswith("."):
        raise SnapshotError("%s 非法：只允许字母数字与 _.:- ，长度 1..128" % field)
    return value


def save(conn, root, msg_id: str, face_id: str, frame_ts: Any, data: bytes,
         max_bytes: int, now: Optional[int] = None) -> Dict[str, Any]:
    """落盘并登记一张照片，返回登记结果（含相对路径）。

    同一 `msg_id` 重复上传：不覆盖、不重复登记，返回既有记录（幂等）。
    """
    _check_id(str(msg_id), "msg_id")
    _check_id(str(face_id), "face_id")
    try:
        frame_ts_int = int(frame_ts)
    except (TypeError, ValueError):
        raise SnapshotError("frame_ts 必须是毫秒整数")
    if not isinstance(data, (bytes, bytearray)) or not data:
        raise SnapshotError("照片内容为空")
    if len(data) > int(max_bytes):
        raise SnapshotError("照片超过上限 %d 字节" % int(max_bytes))

    existing = store.get_snapshot(conn, msg_id)
    if existing is not None:
        return {"msg_id": msg_id, "status": existing["status"],
                "path": existing["path"], "bytes": int(existing["bytes"]),
                "duplicate": True}

    ts = int(time.time() * 1000) if now is None else int(now)
    day = time.strftime(_DAY_FMT, time.localtime(frame_ts_int / 1000))
    root = Path(root)
    day_dir = root / day
    day_dir.mkdir(parents=True, exist_ok=True)
    target = day_dir / (str(msg_id) + ".jpg")

    tmp = day_dir / (".%s.%d.tmp" % (msg_id, os.getpid()))
    try:
        with open(str(tmp), "wb") as fh:
            fh.write(bytes(data))
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(str(tmp), str(target))
    finally:
        if tmp.exists():
            try:
                tmp.unlink()
            except OSError:
                pass

    rel = target.relative_to(root).as_posix() if _is_relative(target, root) else str(target)
    store.record_snapshot(conn, msg_id=msg_id, face_id=str(face_id),
                          frame_ts=frame_ts_int, path=rel, nbytes=len(data),
                          status=store.SNAPSHOT_RECEIVED, received_at=ts)
    return {"msg_id": msg_id, "status": store.SNAPSHOT_RECEIVED,
            "path": rel, "bytes": len(data), "duplicate": False}


def _is_relative(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False


def absolute_path(root, rel_path: str) -> Path:
    """登记时存的是相对 snapshots 根的路径；读取时拼回去。"""
    root = Path(root)
    return root / str(rel_path)


def photo_for_visit(conn, visit_id: str) -> Optional[Dict[str, Any]]:
    """该次到访"最早一条已收到"的照片（设计 §5 的确定性选图）。"""
    row = store.photo_for_visit(conn, visit_id)
    if row is None:
        return None
    return {"msg_id": str(row["msg_id"]), "face_id": str(row["face_id"]),
            "frame_ts": int(row["frame_ts"]), "path": str(row["path"]),
            "bytes": int(row["bytes"]), "received_at": int(row["received_at"])}