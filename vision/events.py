"""消息构造 —— 严格对齐 docs/02 §2/§3（毫秒时间戳、uuid、UTF-8 JSON 文本帧）。

**为什么单独一个模块**：协议是本项目最容易出错的地方（秒/毫秒混用、字段漏 null），
把它做成纯函数后，两侧都能在单测里钉死，联调时不靠"看着差不多"。
"""
import time
import uuid

# docs/02 §3.1 的必填字段（bbox 可选）
FACE_EVENT_REQUIRED = (
    "type", "msg_id", "ts", "face_id", "is_known", "name", "confidence", "frame_ts",
)


def now_ms():
    """毫秒时间戳。**全项目只用这一个单位**，别在别处写 `time.time()`。"""
    return int(time.time() * 1000)


def new_msg_id():
    return str(uuid.uuid4())


def face_event(face_id, is_known, name, confidence, bbox=None, frame_ts=None, ts=None):
    """构造 `face_event`（docs/02 §3.1）。

    - `frame_ts`：人脸被捕获的时刻；`ts`：发送时刻。两者分开是协议要求
      （处理有延迟，越权告警要按帧时间排序）
    - `name` 必须显式给 `None`（陌生人也带字段），不能省 —— 消费方按字段存在与否判断
    """
    frame_ts = now_ms() if frame_ts is None else int(frame_ts)
    msg = {
        "type": "face_event",
        "msg_id": new_msg_id(),
        "ts": now_ms() if ts is None else int(ts),
        "face_id": str(face_id),
        "is_known": bool(is_known),
        "name": None if name is None else str(name),
        "confidence": round(_clamp01(confidence), 3),
        "frame_ts": frame_ts,
    }
    if bbox is not None:
        msg["bbox"] = [int(round(v)) for v in bbox]
    return msg


def heartbeat(fps, frame_drop=0, ts=None):
    """`vision_heartbeat`（docs/02 §3.2）。对话节点 15 秒收不到就报"视觉节点离线"。"""
    return {
        "type": "vision_heartbeat",
        "msg_id": new_msg_id(),
        "ts": now_ms() if ts is None else int(ts),
        "fps": round(float(fps), 1),
        "frame_drop": int(frame_drop),
    }


def _clamp01(value):
    try:
        v = float(value)
    except (TypeError, ValueError):
        return 0.0
    return 0.0 if v < 0.0 else (1.0 if v > 1.0 else v)


def check(msg):
    """返回问题列表（空列表 = 合规）。服务端和测试都用它，避免"样例对、实现错"。"""
    problems = []
    if not isinstance(msg, dict):
        return ["消息不是 dict"]

    kind = msg.get("type")
    if kind == "face_event":
        for key in FACE_EVENT_REQUIRED:
            if key not in msg:
                problems.append("缺少字段 %s" % key)
        if not isinstance(msg.get("ts"), int) or not isinstance(msg.get("frame_ts"), int):
            problems.append("ts / frame_ts 必须是 int（毫秒）")
        if msg.get("ts") and msg.get("ts") < 10 ** 12:
            problems.append("ts 看起来是秒级，必须是毫秒")
        if msg.get("is_known") and not msg.get("name"):
            problems.append("is_known=true 时必须带 name")
        if msg.get("is_known") is False and msg.get("name") is not None:
            problems.append("陌生人的 name 必须是 null")
    elif kind == "vision_heartbeat":
        for key in ("type", "msg_id", "ts", "fps", "frame_drop"):
            if key not in msg:
                problems.append("缺少字段 %s" % key)
    else:
        problems.append("未知 type=%r" % (kind,))
    return problems
