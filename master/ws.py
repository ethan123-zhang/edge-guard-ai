"""主人端连接注册与广播（Web 看板 / APK 共用）。

与视觉侧的 `vision/ws_client.py` 相反，这里是**服务端**：
- 连接前必须过 token 鉴权（`api.py` 里完成，这里只管已鉴权的连接）；
- 广播是尽力而为：某个客户端写失败就摘掉，不能拖累其它客户端和主循环；
- 每个连接一把发送锁：后台 worker 线程与请求线程都会推送。
"""
from __future__ import annotations

import socket
import threading
import time
from typing import Any, Callable, Dict, List, Optional

from vision import ws_protocol


class Client(object):
    __slots__ = ("sock", "device_id", "addr", "send_lock", "closed", "connected_at")

    def __init__(self, sock, device_id: str, addr, send_timeout: float = 1.0):
        self.sock = sock
        self.device_id = device_id
        self.addr = addr
        self.send_lock = threading.Lock()
        self.closed = False
        self.connected_at = time.time()
        try:
            sock.settimeout(send_timeout)
        except OSError:
            pass

    def send_json(self, payload: Dict[str, Any]) -> bool:
        if self.closed:
            return False
        text = ws_protocol.encode_frame(_dumps(payload), ws_protocol.OP_TEXT, mask=False)
        with self.send_lock:
            try:
                self.sock.sendall(text)
                return True
            except OSError:
                self.closed = True
                return False

    def close(self) -> None:
        if self.closed:
            return
        self.closed = True
        try:
            self.sock.close()
        except OSError:
            pass


def _dumps(payload: Dict[str, Any]) -> str:
    import json

    return json.dumps(payload, ensure_ascii=False, sort_keys=True)


class ClientHub(object):
    """已连接主人端的注册表。所有方法线程安全。"""

    def __init__(self, log: Optional[Callable[[str], None]] = None):
        self._lock = threading.Lock()
        self._clients: List[Client] = []
        self._log = log or (lambda message: None)

    def register(self, sock, device_id: str, addr) -> Client:
        client = Client(sock, device_id, addr)
        with self._lock:
            self._clients.append(client)
        self._log("[ws] 主人端连接 %s（device=%s，当前 %d）"
                  % (addr, device_id, self.count()))
        return client

    def unregister(self, client: Client) -> None:
        with self._lock:
            if client in self._clients:
                self._clients.remove(client)
        client.close()
        self._log("[ws] 主人端断开（device=%s，当前 %d）"
                  % (client.device_id, self.count()))

    def count(self) -> int:
        with self._lock:
            return len(self._clients)

    def broadcast(self, payload: Dict[str, Any]) -> int:
        """发给所有连接，返回成功送达的连接数（`LanAdapter` 用它判断"受理"）。"""
        with self._lock:
            clients = list(self._clients)
        delivered = 0
        dead = []
        for client in clients:
            if client.send_json(payload):
                delivered += 1
            else:
                dead.append(client)
        for client in dead:
            self.unregister(client)
        return delivered

    def close_all(self) -> None:
        with self._lock:
            clients = list(self._clients)
            self._clients = []
        for client in clients:
            client.close()