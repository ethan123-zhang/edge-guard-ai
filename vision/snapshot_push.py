"""存图后把 JPEG 推到主人端（`POST /vision/snapshot`，设计 §5）。

为什么单独一个后台线程而不是在 `_report` 里同步发：
- 主循环 17 fps，一条 POST 要建 TCP、写 ~100 KB、读响应；对端没起/accept 了不应答时
  同步发会把帧率拖到 8 fps 以下（`ws_client.py` 2026-11-07 已在事件通道踩过同一个坑）。
- 视觉节点是生产者：**推图失败不能影响上报事件**，更不能阻塞主循环。

约定：
- `enqueue()` 永不阻塞、永不抛：队列满丢最新的并计入 `dropped`。
- 每次请求全程受 `timeout` 约束（连接/写/读），失败只记账，工作线程不死。
- `close()` 先把队列里剩下的发完（有上限）再退线程；主循环 `finally` 里调用。

照片归属只认事件 `msg_id`（任务书 §二.4）：查询串里的 `msg_id` 必须就是触发这张图的
`face_event` 的 `msg_id`，绝不让主人端按文件名/时间猜。
"""
import http.client
import os
import queue
import threading
import time
from urllib.parse import urlencode, urlparse


class SnapshotPushError(RuntimeError):
    """对端返回非 2xx（参数/鉴权/服务端错误）。只用于记账。"""


class SnapshotPusher(object):
    """有界队列 + 守护线程的图片推送器。"""

    def __init__(self, url, key, timeout=2.0, queue_size=8, log=None):
        self.url = str(url)
        self.key = str(key or "")
        self.timeout = float(timeout)
        self.queue_size = int(queue_size)
        self.log = log or (lambda msg: None)
        if self.timeout <= 0:
            raise ValueError("timeout 必须为正（秒），当前 %r" % (timeout,))
        if self.queue_size < 1:
            raise ValueError("queue_size 至少为 1，当前 %r" % (queue_size,))
        if not self.key:
            raise ValueError("缺少共享密钥（X-Vision-Key）")
        self._parse_url(self.url)

        self.sent = 0
        self.failed = 0
        self.dropped = 0
        self.last_error = ""
        self._closed = False
        self._queue = queue.Queue(maxsize=self.queue_size)
        self._thread = threading.Thread(target=self._run, name="snapshot-pusher")
        self._thread.daemon = True
        self._thread.start()

    def _parse_url(self, url):
        parsed = urlparse(url)
        if parsed.scheme not in ("http",):
            raise ValueError("只支持 http://（局域网节点，当前 %r）" % (url,))
        if not parsed.hostname:
            raise ValueError("URL 里没有主机名：%r" % (url,))
        if parsed.query or parsed.fragment:
            raise ValueError("URL 不要带查询串/片段（查询参数由推送器拼）：%r" % (url,))
        self._host = parsed.hostname
        self._port = int(parsed.port or 80)
        self._path = parsed.path or "/"

    # ---------------------------------------------------------------- 对外

    def enqueue(self, msg_id, face_id, frame_ts, data):
        """入队一张待推的照片。永不阻塞/抛异常；队列满返回 False 并计 `dropped`。"""
        if self._closed:
            self.dropped += 1
            return False
        if not data:
            self.dropped += 1
            return False
        item = {
            "msg_id": str(msg_id),
            "face_id": str(face_id),
            "frame_ts": int(frame_ts),
            "data": data,
        }
        try:
            self._queue.put_nowait(item)
            return True
        except queue.Full:
            self.dropped += 1
            if self.dropped <= 3 or self.dropped % 50 == 0:
                self.log("[vision] 推图队列已满（%d 条），丢弃 msg_id=%s（累计丢弃 %d）"
                         % (self.queue_size, item["msg_id"], self.dropped))
            return False

    def close(self, timeout=5.0):
        """等队列发完（最多 `timeout` 秒）再退线程。可重复调用。"""
        if self._closed:
            return
        self._closed = True
        try:
            self._queue.put(None, timeout=timeout)
        except queue.Full:
            # 队列塞满时先不塞哨兵：工作线程排空后 close 再补一次
            pass
        if self._thread is not None:
            self._thread.join(timeout)
            if self._thread.is_alive():
                self.log("[vision] 推图线程未在 %.1f 秒内退出（可能还在等对端）" % timeout)

    def thread_alive(self):
        return self._thread is not None and self._thread.is_alive()

    def describe(self):
        return "%s（成功 %d / 失败 %d / 丢弃 %d）" % (
            self.url, self.sent, self.failed, self.dropped)

    # ---------------------------------------------------------------- 工作线程

    def _run(self):
        while True:
            item = self._queue.get()
            if item is None:
                self._queue.task_done()
                return
            try:
                self._post(item)
                self.sent += 1
                if self.last_error:
                    self.log("[vision] 推图恢复（之前失败：%s）" % self.last_error)
                    self.last_error = ""
            except (OSError, http.client.HTTPException, SnapshotPushError) as exc:
                self.failed += 1
                self.last_error = str(exc)
                if self.failed <= 3 or self.failed % 50 == 0:
                    self.log("[vision] 推图失败 msg_id=%s：%s（累计失败 %d）"
                             % (item["msg_id"], exc, self.failed))
            finally:
                self._queue.task_done()

    def _post(self, item):
        """一次 POST；任何失败都抛异常（由 `_run` 记账），成功返回 None。"""
        query = urlencode([
            ("msg_id", item["msg_id"]),
            ("face_id", item["face_id"]),
            ("frame_ts", item["frame_ts"]),
        ])
        path = "%s?%s" % (self._path, query)
        conn = http.client.HTTPConnection(self._host, self._port, timeout=self.timeout)
        try:
            conn.putrequest("POST", path)
            conn.putheader("X-Vision-Key", self.key)
            conn.putheader("Content-Type", "image/jpeg")
            conn.putheader("Content-Length", str(len(item["data"])))
            conn.endheaders(item["data"])
            response = conn.getresponse()
            response.read()          # 响应很小，读掉再 close，避免连接半关
            if response.status < 200 or response.status >= 300:
                raise SnapshotPushError("HTTP %d" % response.status)
        finally:
            conn.close()


def build_pusher(cfg, log=None):
    """按 `output.snapshot_push` 造推送器；未启用/缺密钥/配置有误 → None（只打日志）。"""
    log = log or (lambda msg: None)
    push = (cfg.get("output") or {}).get("snapshot_push") or {}
    if not bool(push.get("enabled", False)):
        return None
    url = str(push.get("url") or "").strip()
    key = str(push.get("key") or "").strip()
    if not key:
        key = os.environ.get("SENTINEL_VISION_KEY", "").strip()
    if not key:
        log("[vision] 推图未启用：缺少共享密钥（写 output.snapshot_push.key "
            "或环境变量 SENTINEL_VISION_KEY）")
        return None
    try:
        return SnapshotPusher(url, key,
                              timeout=float(push.get("timeout", 2.0)),
                              queue_size=int(push.get("queue_size", 8)),
                              log=log)
    except ValueError as exc:
        log("[vision] 推图未启用：配置有误（%s）" % exc)
        return None