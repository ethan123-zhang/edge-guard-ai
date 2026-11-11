"""推送适配器接口。

`send(message_key, payload) -> (ok, error)`：
- `ok=True` 才能把 outbox 那行置 `sent`；返回即"已受理"，**不等于**手机已展示。
- 适配器不许改消息内容（幂等键与 payload 都是冻结的）。

已定通道：`lan`（WebSocket 广播给已连接的主人端）。
通道定了再插：`relay`（公网中转）/ `vendor`（厂商推送 SDK）—— 各一个文件，
`master/config.py` 的 `push.adapter` 选一个即可，不改调用方。
"""
from __future__ import annotations


class PushAdapter(object):
    """所有适配器的基类：只要求一个 `send`。"""

    name = "base"

    def send(self, message_key, payload):
        raise NotImplementedError

    def close(self):
        """进程退出时清理（默认什么都不做）。"""


def create(adapter_name, **kwargs):
    """按名字造适配器。`lan` 已实现；其余名字在第二阶段接通道时再补。"""
    name = str(adapter_name or "").strip().lower()
    if name == "lan":
        from master.push.lan import LanAdapter
        return LanAdapter(**kwargs)
    raise ValueError("未知推送适配器 `%s`（当前支持：lan）" % adapter_name)