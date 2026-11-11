"""局域网直连适配器：把消息广播给所有已连接的主人端（Web 看板 / APK）。

`push_accept` 在这里的语义是"至少有一个接入方在线并已受理"：
没有客户端在线时返回 False，消息留在 outbox 里等下次重连补发 —— 这正是断网补传的入口。
"""
from __future__ import annotations

from master.push import PushAdapter


class LanAdapter(PushAdapter):
    name = "lan"

    def __init__(self, broadcast, min_clients: int = 1):
        """`broadcast(payload) -> 送达客户端数`，由 `master/ws.py` 提供（线程安全）。"""
        if not callable(broadcast):
            raise ValueError("LanAdapter 需要 broadcast 回调")
        self._broadcast = broadcast
        self._min_clients = max(1, int(min_clients))

    def send(self, message_key, payload):
        clients = self._broadcast(payload)
        if int(clients) < self._min_clients:
            return False, "当前无在线主人端（局域网直连），等待重连补发"
        return True, None