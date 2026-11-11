"""鉴权：设备 token（库里只存 sha256）+ 照片 URL 短时签名。

两条不可退让的规则（设计 §6）：
1. **不存明文 token**：库里只有 `sha256(salt + token)`，盐来自环境变量。
2. **密钥未配置就拒绝服务**，不静默放行 —— 空串不是"不限"。
"""
from __future__ import annotations

import hashlib
import hmac
import secrets
import time
from typing import Optional

from master import store

BEARER_PREFIX = "Bearer "


class AuthError(Exception):
    """鉴权失败；调用方转 401/403。"""


def _require_secret(value: str, name: str) -> str:
    if not value:
        raise AuthError("%s 未配置（环境变量或 config/master.local.yaml）" % name)
    return value


def hash_token(token: str, salt: str) -> str:
    return hashlib.sha256((salt + token).encode("utf-8")).hexdigest()


def new_token() -> str:
    """设备 token 明文，只在登记时返回一次。"""
    return secrets.token_urlsafe(32)


def issue_device(conn, device_id: str, name: str, salt: str) -> str:
    """登记/重发一个设备的 token，返回**明文**（库里只存哈希）。"""
    salt = _require_secret(salt, "security.token_salt")
    token = new_token()
    store.upsert_device(conn, device_id=device_id, name=name[:64],
                        token_hash=hash_token(token, salt))
    return token


def authenticate(conn, token: Optional[str], salt: str):
    """校验 Bearer token，成功返回设备行，失败返回 None。

    未配置盐时抛 AuthError —— 配置缺失是要立刻发现的部署错误，
    不能伪装成"这个客户端没有权限"。
    """
    salt = _require_secret(salt, "security.token_salt")
    if not token:
        return None
    row = store.device_by_token_hash(conn, hash_token(token, salt))
    if row is None:
        return None
    store.touch_device(conn, str(row["device_id"]))
    return row


def check_vision_key(provided: Optional[str], expected: str) -> bool:
    """Jetson 上传照片的共享密钥校验（常量时间比较，避免时序侧信道）。"""
    if not expected or not provided:
        return False
    return hmac.compare_digest(str(provided), str(expected))


# ---------------------------------------------------------------- 照片签名

def photo_signature(secret: str, visit_id: str, msg_id: str, exp: int) -> str:
    """`HMAC-SHA256(secret, "visit_id|msg_id|exp")`，十六进制。"""
    secret = _require_secret(secret, "security.photo_secret")
    message = "%s|%s|%d" % (visit_id, msg_id, int(exp))
    return hmac.new(secret.encode("utf-8"), message.encode("utf-8"),
                    hashlib.sha256).hexdigest()


def verify_photo_signature(secret: str, visit_id: str, msg_id: str, exp: int,
                           signature: Optional[str], now: Optional[int] = None) -> bool:
    if not signature:
        return False
    ts = int(time.time()) if now is None else int(now)
    if int(exp) < ts:
        return False
    try:
        expected = photo_signature(secret, visit_id, msg_id, int(exp))
    except AuthError:
        return False
    return hmac.compare_digest(str(signature), expected)


def signed_photo_query(secret: str, visit_id: str, msg_id: str,
                       ttl_s: int, now: Optional[int] = None) -> str:
    """给推送消息用的相对 URL：`?exp=...&sig=...`（手机拼上服务器地址即可）。"""
    ts = int(time.time()) if now is None else int(now)
    exp = ts + int(ttl_s)
    return "exp=%d&sig=%s" % (exp, photo_signature(secret, visit_id, msg_id, exp))