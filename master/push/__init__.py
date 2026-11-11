"""推送适配器接口。

`send(message_key, payload) -> (ok, error)`：
- `ok=True` 才能把 outbox 那行置 `sent`；返回即"已受理"，**不等于**手机已展示。
- 适配器不许改消息内容（幂等键与 payload 都是冻结的）。

当前已实现的通道：
- `lan`：局域网 WebSocket 广播（看板 / APK）
- `relay`：公网 HTTP 推送（企业微信群机器人；通用模板可换飞书/钉钉/PushPlus）
- `composite`：两者同发，任一受理即算送达（手机在不在局域网都能收到）
"""
from __future__ import annotations


class PushAdapter(object):
    """所有适配器的基类：只要求一个 `send`。"""

    name = "base"

    def send(self, message_key, payload):
        raise NotImplementedError

    def close(self):
        """进程退出时清理（默认什么都不做）。"""


def _build_one(name, cfg, broadcast, log):
    name = str(name or "").strip().lower()
    relay_cfg = (cfg or {}).get("push", {}).get("relay", {}) or {}
    if name == "lan":
        from master.push.lan import LanAdapter
        if not callable(broadcast):
            raise ValueError("lan 适配器需要 broadcast 回调")
        return LanAdapter(broadcast)
    if name == "relay":
        from master.push.relay import RelayAdapter
        url = str(relay_cfg.get("url") or "").strip()
        if not url:
            log("[push] relay 未配置 webhook URL，跳过公网通道")
            return None
        return RelayAdapter(url=url,
                            provider=relay_cfg.get("provider", "wecom"),
                            timeout_s=float(relay_cfg.get("timeout_s", 5)),
                            log=log)
    raise ValueError("未知推送适配器 `%s`（当前支持：lan / relay）" % name)


def create(adapter_name, cfg=None, broadcast=None, log=None):
    """按名字（或名字列表）造适配器。多个通道时包成 composite。"""
    log = log or (lambda message: None)
    names = adapter_name if isinstance(adapter_name, (list, tuple)) else [adapter_name]
    built = []
    for name in names:
        adapter = _build_one(name, cfg, broadcast, log)
        if adapter is not None:
            built.append(adapter)
    if not built:
        raise ValueError("没有可用的推送通道（检查 push.adapter 与 push.relay.url）")
    if len(built) == 1:
        return built[0]
    from master.push.composite import CompositeAdapter
    return CompositeAdapter(built, log=log)