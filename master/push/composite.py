"""组合适配器：同时投局域网（看板/APK）与公网（企业微信）。

语义（2026-11-12 起改成 fan-out）：**每个通道都投**，任一通道受理就算送达，两条都失败才重试。
为什么不再"首个成功就返回"：演示现场是「PC 投屏看板在线 + 手机锁屏靠企业微信收通知」同时进行，
LAN 先成功就 return 会让企业微信永远不发 —— 2026-11-12 S2 实测：outbox 记 `sent`，
锁屏手机一条都没收到。代价是单条消息里某一路失败不会单独重试，只记日志。
"""
from __future__ import annotations

from master.push import PushAdapter


class CompositeAdapter(PushAdapter):
    name = "composite"

    def __init__(self, adapters, log=None):
        self.adapters = [adapter for adapter in adapters if adapter is not None]
        if not self.adapters:
            raise ValueError("CompositeAdapter 至少要有一个子适配器")
        self._log = log or (lambda message: None)

    def send(self, message_key, payload):
        delivered = False
        errors = []
        for adapter in self.adapters:
            name = getattr(adapter, "name", "?")
            try:
                ok, error = adapter.send(message_key, payload)
            except Exception as exc:  # 单个适配器异常不能拖垮另一个通道
                ok, error = False, "%s: %s" % (type(exc).__name__, exc)
            if ok:
                delivered = True
                continue
            errors.append("%s=%s" % (name, error))
            # 失败不重试整条消息（另一路已经送达），但要留痕便于现场排查
            self._log("[push] %s 经 %s 投递失败：%s" % (message_key, name, error))
        if delivered:
            return True, None
        return False, "；".join(errors)