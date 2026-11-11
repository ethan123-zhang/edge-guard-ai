"""组合适配器：同时投局域网（看板/APK）与公网（企业微信）。

语义：**任一通道受理就算送达**，两条都失败才重试。
理由：手机在局域网时看板/APK 能收；不在局域网时企业微信能收；两条都断才需要补发。
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
        errors = []
        for adapter in self.adapters:
            try:
                ok, error = adapter.send(message_key, payload)
            except Exception as exc:  # 单个适配器异常不能拖垮另一个通道
                ok, error = False, "%s: %s" % (type(exc).__name__, exc)
            if ok:
                return True, None
            errors.append("%s=%s" % (getattr(adapter, "name", "?"), error))
        return False, "；".join(errors)