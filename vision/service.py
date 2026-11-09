"""视觉主循环：读帧 → 检测 → 识别 → 稳定 → 上报（+ 心跳 + 截图）。

`Sink` 是唯一的出口：`WsSink` 发给对话节点，`JsonlSink` 落文件（联调用），
`NullSink` 什么都不做（压测/单测）。这样主循环本身完全不依赖网络。

**两层去重**（契约见 `docs/跨轨关联与两层去重合同-2026-11-01.md`）：
1. 空间轨道层（`vision/tracker.py`）：一条轨道只报一次；
2. 匿名会话层（`vision/association.py`）：轨道断了又回来（同一个人出画再入画、
   走路抖动丢检），只要外观证据够好且唯一，就并回同一个会话 —— **该会话报过就不再报**。
已知人脸不走第 2 层：身份去重是业务层（10 分钟到访）的事，不是视觉层该做的。
"""
import json
import os
import time

import cv2
import numpy as np

from vision import align, association, config, events
from vision.imageio import imwrite
from vision.tracker import iou
from vision.ws_client import WebSocketClient, WsClientError

# 同一帧两个框 IoU 超过这个值就认为是同一张脸被重复检出（去重见 `_dedupe_detections`）
DEDUPE_IOU = 0.5

# 帧级均匀保护的探针宽度：整帧缩到 160 宽再算灰度标准差，720p 上约 0.2 ms。
BLANK_PROBE_WIDTH = 160

# 首报延迟（见 `_first_report`）里算"弱建会话"的理由：外观证据自认不足，
# 正常外观证据（new_session / associated）攒 K 帧，这些要攒更久。
DEFER_WEAK_REASONS = ("below_tau", "ambiguous_margin", "session_occupied")


def face_label(face_id_or_event):
    """陌生人的人脸"标签"统一成 `?`：不受 tracker 的 unk_N 轨道编号影响。

    事件上的 `face_id` 对陌生人是 tracker 给的轨道稳定 id（unk_0…），而帧内
    `face["face_id"]` 是 None；两者拿来做"还是不是同一个人"的比较必须先对齐。
    """
    face_id = getattr(face_id_or_event, "face_id", face_id_or_event)
    return face_id or "?"


# ---------------------------------------------------------------- Sink

class NullSink(object):
    name = "null"

    def send(self, msg):
        return True

    def close(self):
        pass


class JsonlSink(object):
    """事件按行写文件，带毫秒时间戳。联调时 `tail -f` 就能看。"""

    name = "jsonl"

    def __init__(self, path, log=None):
        self.path = str(path)
        parent = os.path.dirname(os.path.abspath(self.path))
        if parent:
            os.makedirs(parent, exist_ok=True)
        self._fh = open(self.path, "a", encoding="utf-8")
        self.log = log or (lambda msg: None)

    def send(self, msg):
        self._fh.write(json.dumps(msg, ensure_ascii=False) + "\n")
        self._fh.flush()
        return True

    def close(self):
        try:
            self._fh.close()
        except OSError:
            pass


class WsSink(object):
    """把事件 JSON 发到对话节点。断了不阻塞主循环（`send` 返回 False）。"""

    name = "ws"

    def __init__(self, url, reconnect_sec=3.0, timeout=1.0, send_timeout=0.5,
                 max_reconnect_sec=60.0, log=None):
        self._client = WebSocketClient(url, timeout=timeout, reconnect_sec=reconnect_sec,
                                       send_timeout=send_timeout,
                                       max_reconnect_sec=max_reconnect_sec, log=log)
        self.log = log or (lambda msg: None)

    def send(self, msg):
        return self._client.send(json.dumps(msg, ensure_ascii=False))

    def close(self):
        self._client.close()

    def describe(self):
        return self._client.describe()


class MultiSink(object):
    """一个事件发给多个出口（WS + JSONL）。**任一出口失败就算失败**，便于发现对端挂了。"""

    name = "multi"

    def __init__(self, sinks):
        self._sinks = [s for s in sinks if s is not None]
        self.name = "+".join(getattr(s, "name", "?") for s in self._sinks)

    def send(self, msg):
        ok = True
        for sink in self._sinks:
            if not sink.send(msg):
                ok = False
        return ok

    def close(self):
        for sink in self._sinks:
            sink.close()

    def describe(self):
        return " / ".join(getattr(s, "name", "?") for s in self._sinks)


# ---------------------------------------------------------------- 统计

class Stats(object):
    def __init__(self):
        self.frames = 0
        self.events = 0
        self.sent = 0
        self.failed = 0
        self.dropped = 0
        self.suppressed = 0     # 关联层判为「同一会话已报过」而没发的条数
        self.deferred = 0       # 现挂会话首报还在等确认 / 确认不足被撤销的条数
        self.blank = 0          # 帧级均匀保护跳过的近均匀帧（镜头被遮/全黑/流掉线）
        self.fps = 0.0
        self.infer_ms = 0.0
        self.started_at = time.time()

    def summary(self):
        elapsed = max(1e-6, time.time() - self.started_at)
        blank = " · 空白帧 %d" % self.blank if self.blank else ""
        return ("帧 %d（%.1f fps）· 上报 %d 条（成功 %d / 失败 %d）· "
                "单帧推理 %.1f ms · 丢帧 %d · 关联去重 %d · 首报待确认 %d%s" % (
                    self.frames, self.frames / elapsed, self.events,
                    self.sent, self.failed, self.infer_ms, self.dropped,
                    self.suppressed, self.deferred, blank,
                ))


# ---------------------------------------------------------------- 服务

class VisionService(object):
    def __init__(self, cfg, source, detector, recognizer, gallery, tracker,
                 sink, log=None):
        self.cfg = cfg
        self.source = source
        self.detector = detector
        self.recognizer = recognizer
        self.gallery = gallery
        self.tracker = tracker
        self.sink = sink
        self.log = log or self._default_log
        self.stats = Stats()
        self._last_hb = 0.0
        self._match_threshold = float(cfg["recognition"]["match_threshold"])
        self._min_face_px = int(cfg["detection"]["min_face_px"])
        self._blank_std = float(cfg["detection"].get("blank_std", 0.0) or 0.0)
        self._blank_logged = 0
        self._save_snapshots = bool(cfg["output"]["save_snapshots"])
        self._snapshot_dir = config.resolve_project(cfg["output"]["snapshot_dir"])
        self._snapshot_quality = int(cfg["output"]["snapshot_quality"])
        self._print_events = bool(cfg["output"]["print_events"])
        self._roi = self._roi_rect_from_cfg(cfg["roi"])
        self._snapshot_saved = 0
        self.associator = self._build_associator(cfg.get("association") or {})
        self._reported_sessions = set()
        # 上报前的质量证据门（关联层开着才生效）：2026-11-08 真素材实测，
        # 出画/转身那几帧五点对齐残差 0.21–0.80（甚至 25），框还能检出但特征与参考
        # 差 0.2–0.4，于是每帧新建一条幽灵会话、一次出画多报 3 条"新访客"。
        # 规则：一条轨道至少有一帧过质量门的证据，才允许上报；纯侧脸/残影不产生访客。
        assoc_cfg = cfg.get("association") or {}
        self._quality_max_rms = float(assoc_cfg.get(
            "quality_max_rms", association.DEFAULT_QUALITY_MAX_RMS))
        self._require_quality = (bool(assoc_cfg.get("require_quality_evidence", True))
                                 and self.associator.enabled)
        self._good_quality_tracks = {}   # track_id -> 最后一帧合格证据的时刻
        # 现挂会话首报延迟（2026-11-09，见 _first_report）：会话第一次建出来时先不发，
        # 等同一身份标签再给出 K 帧 / K 毫秒证据（先到者）才真正上报，没撑住的就不发。
        self._defer_frames = max(0, int(assoc_cfg.get(
            "first_report_confirm_frames", 0) or 0))
        self._defer_ms = max(0, int(float(assoc_cfg.get(
            "first_report_confirm_ms", 0) or 0) * 1000.0))
        self._weak_defer_frames = max(self._defer_frames, int(assoc_cfg.get(
            "weak_first_report_confirm_frames", 0) or 0))
        self._pending = {}       # track_id -> 待确认的首报（结构见 _first_report）
        # 本帧被 _first_report 挂起的轨道：它的第 1 帧已经算过，_advance_pending 不再补数
        self._emitted_this_frame = set()
        # 本帧每条轨道这一帧的人脸标签（track_id -> face_id 或 "?"），见 _label_of
        self._frame_labels = {}
        self.backend_label = self._backend_label()

    # ---------------------------------------------------------------- 关联层

    @staticmethod
    def _build_associator(assoc_cfg):
        """按配置造匿名关联器；`enabled` 为假时它自己会原样放行（行为不变）。"""
        return association.AnonymousAssociator(
            enabled=bool(assoc_cfg.get("enabled", False)),
            tau=float(assoc_cfg.get("tau", association.DEFAULT_TAU)),
            margin=float(assoc_cfg.get("margin", association.DEFAULT_MARGIN)),
            quality_max_rms=float(assoc_cfg.get("quality_max_rms",
                                                association.DEFAULT_QUALITY_MAX_RMS)),
            session_ttl_sec=float(assoc_cfg.get("session_ttl_sec",
                                                association.DEFAULT_SESSION_TTL_SEC)),
            track_ttl_sec=float(assoc_cfg.get("track_ttl_sec",
                                              association.DEFAULT_TRACK_TTL_SEC)),
            max_sessions=int(assoc_cfg.get("max_sessions",
                                           association.DEFAULT_MAX_SESSIONS)),
            max_refs=int(assoc_cfg.get("max_refs", association.DEFAULT_MAX_REFS)),
            weak_obs=int(assoc_cfg.get("weak_obs", association.DEFAULT_WEAK_OBS)),
        )

    @staticmethod
    def _quality_of(detection):
        """这张脸的质量指标（对齐残差 ÷ 眼距）；没有关键点（Haar）就是 None。"""
        points = getattr(detection, "landmarks", None)
        if not points:
            return None
        try:
            return align.quality(np.asarray(points, dtype=np.float64).reshape(5, 2))
        except (TypeError, ValueError):
            return None

    def _track_good_quality(self, faces, frame_ts):
        """记下这一帧哪些轨道拿到了合格证据（上报门要用）。"""
        for face in faces:
            track_id = face.get("track_id")
            if track_id is None:
                continue
            rms = face.get("rms_norm")
            if rms is None or rms <= self._quality_max_rms:
                self._good_quality_tracks[int(track_id)] = int(frame_ts)
        if len(self._good_quality_tracks) > 2048:
            cutoff = int(frame_ts) - 120000
            for track_id in list(self._good_quality_tracks.keys()):
                if self._good_quality_tracks[track_id] < cutoff:
                    self._good_quality_tracks.pop(track_id, None)

    def _should_report(self, event, assoc_results):
        """未知人脸过一层匿名会话去重；返回 False 表示这次不该再报。"""
        return self._should_report_inner(event, assoc_results)

    def _should_report_inner(self, event, assoc_results):
        if event.is_known or not self.associator.enabled:
            return True
        if (self._require_quality
                and int(event.track_id) not in self._good_quality_tracks):
            # 全程没有一帧过质量门：出画/转身的残影轨道，不产生"新访客"
            self.log("[vision] 上报：轨道 %s 全程没有合格证据（残影/侧脸）→ 不报"
                     % event.track_id)
            return False
        result = assoc_results.get(event.track_id)
        if result is None or result.session_id is None:
            # 没有轨道 / disabled / invalid_evidence：没证据就不猜，照旧上报
            return True
        if result.associated and result.session_id in self._reported_sessions:
            # 诊断日志必须写清「为什么算同一会话」：沿轨（track_bound）这一支没有比较值，
            # 旧日志只打「同类 — / 异类 —」，现场回看时分不清是没比较还是比了没分数。
            self.log("[vision] 关联层：轨道 %s 并入会话 %s（理由 %s；同类 %s / 异类 %s）→ 不重复上报"
                     % (event.track_id, result.session_id, result.reason,
                        "%.2f" % result.own_best if result.own_best is not None else "—",
                        "%.2f" % result.other_best if result.other_best is not None else "—"))
            self._pending.pop(int(event.track_id), None)
            return False
        return self._first_report(event, result, result.session_id)

    # ---------------------------------------------------------------- 首报延迟

    def _first_report(self, event, result, session_id):
        """现挂会话的首报：先挂起，等同一身份标签再给出 K 帧 / K 毫秒证据（先到者）才真发。

        2026-11-09 真素材复评的残余：出画那一两帧会给出**质量过关的干净正面残影**
        （余弦 0.29–0.38、rms 0.07–0.12，落在同人侧脸与异人的重叠带），
        weak_obs 与上报质量门都拦不住，于是多出一条"新访客"。这类残影的共同点是
        **出现一下就没了**，而真在场的人会继续给证据 —— 所以判据是"活得久"，
        不是"长得像"。代价只有首次上报晚 K 帧（板上 17 fps 时约 0.2 s）。

        每帧都要有证据才算一帧：tracker 一段轨道只发一次事件（`track.emitted`），
        所以"这条轨道还在不在场"由 `_settle_pending` 每帧按帧数（同一帧数两遍会
        让 K 帧少算一帧，`_emitted_this_frame` 负责跳过）。

        `session_id` 为 None（没有会话 / invalid_evidence）、或者关联层关着时，
        照旧立即上报，行为与延迟关闭时完全一样；那条 `return` 必须在挂"标记"之前，
        否则会把一条已经真发的轨道误标成"等确认"，害得它下一帧被少算一帧。
        """
        if not self.associator.enabled or self._defer_frames <= 0:
            return self._commit_report(event, session_id)
        track_id = int(event.track_id)
        if session_id is None:
            return self._commit_report(event, session_id)
        weak = bool(result is not None and result.reason in DEFER_WEAK_REASONS)
        if weak and not self._defer_weak_pending(track_id, event, result, session_id):
            # 外观证据自认不足（低于 tau / 歧义 / 会话被占）且不是视觉层自己攒的候选：
            # 不挂起、也不当"活得久"来确认 —— 它连自己是谁都不确定，多活几帧也不会变清楚。
            # 直接作废它临时建出来的会话（残影不该留下痕），记一笔延迟，不占 suppressed 口径。
            # 例外：视觉层自己攒着的"新访客"候选（weak_obs 正在数）最后一帧也是 below_tau，
            # 那是时段边界，不是"看不出来"，它下面几帧自会给出更硬的证据 —— 只延后不否决。
            self._drop_pending(track_id, event.frame_ts)
            self.associator.drop_session(session_id)
            self.stats.deferred += 1
            self.log("[vision] 弱证据首报不采信（轨道 %s：%s own=%s other=%s）→ 不报"
                     % (track_id, result.reason,
                        "%.2f" % result.own_best if result.own_best is not None else "—",
                        "%.2f" % result.other_best if result.other_best is not None else "—"))
            return False
        if track_id in self._pending:
            # 这一帧它自己也发了事件：新的一段证据，上一条挂起没撑住
            self._drop_pending(track_id, event.frame_ts)
        # 标记给 `_settle_pending`：这条轨道本帧已经在**这里**算过第 1 帧了
        self._emitted_this_frame.add(track_id)
        need_frames = self._weak_defer_frames if weak else self._defer_frames
        self._pending[track_id] = {"event": event, "session": session_id,
                                   "label": self._label_of(event),
                                   "result": result, "first_ts": int(event.frame_ts),
                                   "count": 1, "need": need_frames}
        # 同一会话另有轨道挂着：两条轨道指向同一条会话，先到的算数，那条撤掉
        for other_id in list(self._pending.keys()):
            if other_id != track_id and self._pending[other_id]["session"] == session_id:
                self._drop_pending(other_id, None)
        self.stats.deferred += 1
        return False

    def _defer_weak_pending(self, track_id, event, result, session_id):
        """below_tau 但属于"视觉层正在攒的候选"：挂起等确认，而不是当成残影否决。

        判据只用一个不会反噬的信号：这条会话是不是**这一帧刚由本轨道新建**的。
        关联器的 `pending_evidence` 会把候选藏起来不建会话（`session_id` 为 None），
        所以"这一帧建出来 + 建它的就是本轨道"只会发生在 weak_obs 攒够的那一刻。
        """
        if result.reason != "below_tau" or event.track_id is None:
            return False
        if not self.associator.created_by(session_id, event.track_id, event.frame_ts):
            return False
        return True

    def _label_of(self, event):
        """事件对应的"人脸标签"：本帧这条轨道**这一帧**真的认出谁（`_frame_labels`）。

        不能用 `event.face_id`：陌生人的事件标签是 tracker 给的轨道稳定 id（unk_0…），
        而帧内的 `face["face_id"]` 是 None —— 拿它们比"还是不是同一个人"永远不等。
        """
        return self._frame_labels.get(int(event.track_id), face_label(event))

    def _drop_pending(self, track_id, frame_ts):
        """撤掉一条没撑住的现挂首报；它建的会话也一起作废（残影不该留下会话）。"""
        pending = self._pending.pop(int(track_id), None)
        if pending is None:
            return
        self.stats.deferred += 1
        session_id = pending.get("session")
        if session_id is not None and session_id not in self._reported_sessions:
            self.associator.drop_session(session_id)
        held = int(pending["first_ts"])
        if frame_ts is not None:
            held = max(0, int(frame_ts) - held)
        self.log("[vision] 首报未确认，丢弃（轨道 %s，会话 %s，撑了 %d ms）：残影不算访客"
                 % (track_id, session_id, held))

    def _expire_pending(self, frame_ts):
        """超时还没撑满的首报：一律当作残影丢掉（不再无人收割地挂在内存里）。

        时间线跟 `_settle_pending` 取同一个窗口 `_defer_ms`：超过 K 毫秒
        既没攒够帧、也从没落到"时间到了也发"那一步，就是出画了，直接作废 ——
        不留"挂着等下一个字面凑帧"的尾巴。
        上下都夹住：太短会误杀"漏检一帧"，太长（测试/调参把 K 配到 10 s）会让残影
        在内存里住十几秒。
        """
        frame_ts = int(frame_ts)
        timeout = max(1000, min(self._defer_ms, 2000))
        for track_id in list(self._pending.keys()):
            if (frame_ts - self._pending[track_id]["first_ts"]) >= timeout:
                self._drop_pending(track_id, frame_ts)

    def _commit_report(self, event, session_id):
        """真发一条：占住会话 id，之后同一会话的轨道就不再报。"""
        if session_id is not None:
            self._reported_sessions.add(session_id)
        return True

    # ---------------------------------------------------------------- 配置派生

    def _backend_label(self):
        """写进心跳的「实际后端」，例如 `yunet-onnx/tensorrt`、`haar`。"""
        name = str(getattr(self.detector, "backend", "?"))
        accelerator = getattr(self.detector, "accelerator", None)
        return "%s/%s" % (name, accelerator) if accelerator else name

    @staticmethod
    def _roi_rect_from_cfg(roi_cfg):
        if not roi_cfg["enabled"]:
            return None
        return (float(roi_cfg["x"]), float(roi_cfg["y"]),
                float(roi_cfg["w"]), float(roi_cfg["h"]))

    def _crop_rect(self, shape):
        """归一化 ROI → 像素矩形 `(x, y, w, h)`；未启用时返回整帧。"""
        height, width = shape[:2]
        if self._roi is None:
            return (0, 0, width, height)
        rx, ry, rw, rh = self._roi
        x = int(round(rx * width))
        y = int(round(ry * height))
        w = max(1, int(round(rw * width)))
        h = max(1, int(round(rh * height)))
        x = min(max(0, x), width - 1)
        y = min(max(0, y), height - 1)
        w = min(w, width - x)
        h = min(h, height - y)
        return (x, y, w, h)

    # ---------------------------------------------------------------- 主循环

    def run(self, duration_sec=None, on_frame=None, should_stop=None):
        runtime = self.cfg["runtime"]
        duration = float(duration_sec if duration_sec is not None else runtime["duration_sec"])
        deadline = (time.time() + duration) if duration > 0 else None
        max_fps = float(runtime["max_fps"])
        frame_interval = (1.0 / max_fps) if max_fps > 0 else 0.0
        heartbeat_sec = float(self.cfg["server"]["heartbeat_sec"])
        server_enabled = bool(self.cfg["server"]["enabled"])

        self.source.open()
        self.log("[vision] 视频源：%s" % self.source.describe())
        self.log("[vision] 检测器：%s" % self.detector.describe())
        self.log("[vision] 识别器：%s" % self.recognizer.describe())
        self.log("[vision] 实际后端：%s（心跳里会带这个字段）" % self.backend_label)
        self.log("[vision] 人脸库：%d 人" % len(self.gallery))
        self.log("[vision] 事件出口：%s" % getattr(self.sink, "name", "?"))
        self.log("[vision] 关联层：%s" % (
            "开（tau=%.2f, margin=%.2f, 质量门 rms<=%.2f）"
            % (self.associator.tau, self.associator.margin,
               self.associator.quality_max_rms)
            if self.associator.enabled else "关（同一个人出画再回来会重报）"))
        self.log("[vision] 首报延迟：%s" % (
            "开（正常外观 %d 帧 / 弱证据 %d 帧 / 最多 %d ms，先到者为准）"
            % (self._defer_frames, self._weak_defer_frames, self._defer_ms)
            if self.associator.enabled and self._defer_frames > 0
            else "关（会话建出来当帧就上报）"))

        fps_window_start = time.time()
        fps_window_frames = 0
        consecutive_failures = 0

        try:
            while True:
                if deadline is not None and time.time() >= deadline:
                    break
                if should_stop is not None and should_stop():
                    break

                loop_start = time.time()
                ok, frame, frame_ts = self.source.read()
                if not ok or frame is None:
                    self.stats.dropped += 1
                    consecutive_failures += 1
                    if consecutive_failures >= 50:
                        self.log("[vision] 连续 %d 帧读不到，退出（摄像头被拔/文件读完）"
                                 % consecutive_failures)
                        break
                    time.sleep(0.01)
                    continue
                consecutive_failures = 0

                infer_start = time.time()
                annotated, detections = self._process_frame(frame, frame_ts)
                infer_ms = (time.time() - infer_start) * 1000.0
                # 指数滑动平均，避免单帧抖动让数字乱跳
                self.stats.infer_ms = (0.8 * self.stats.infer_ms + 0.2 * infer_ms
                                       if self.stats.frames else infer_ms)
                self.stats.frames += 1
                fps_window_frames += 1

                now = time.time()
                elapsed_window = now - fps_window_start
                if elapsed_window >= 1.0:
                    self.stats.fps = fps_window_frames / elapsed_window
                    fps_window_frames = 0
                    fps_window_start = now

                if server_enabled and (now - self._last_hb) >= heartbeat_sec:
                    self._last_hb = now
                    self._emit(events.heartbeat(self.stats.fps, self.stats.dropped,
                                                backend=self.backend_label))

                if on_frame is not None:
                    on_frame(annotated, detections, self.stats)

                if frame_interval > 0:
                    spare = frame_interval - (time.time() - loop_start)
                    if spare > 0:
                        time.sleep(spare)
        finally:
            self.source.release()
            self.sink.close()
        return self.stats

    # ---------------------------------------------------------------- 单帧处理

    @staticmethod
    def _dedupe_detections(detections, iou_threshold=DEDUPE_IOU):
        """同一张脸被检出两次（幽灵框 / NMS 残留）时只留分数最高的那个。

        为什么要管：幽灵框会以"另一条轨道"的身份拿到同一张脸的特征，把匿名会话复制成
        两份；之后同一个人会同时像两条会话（own / other 都 0.85+），关联层只能判歧义、
        再新建会话 —— 事件数就弹回去了（2026-11-07 板上 287 点回放实测）。
        两个人真正的脸挨不到 IoU 0.5（并排通常在 0.2 以下）。
        """
        kept = []
        for detection in sorted(detections, key=lambda item: item.score, reverse=True):
            if any(iou(detection.bbox, other.bbox) > iou_threshold for other in kept):
                continue
            kept.append(detection)
        return kept

    def _process_frame(self, frame, frame_ts):
        height, width = frame.shape[:2]
        cx, cy, cw, ch = self._crop_rect(frame.shape)
        crop = frame[cy:cy + ch, cx:cx + cw]

        # 帧级均匀保护：近均匀帧（镜头被遮/全黑/流掉线）根本没有可检的人脸，
        # 而 YuNet 在这种输入上会吐"高置信大框"假脸（板端实测 score 0.886）。
        # 这里直接跳过检测+识别，只把空结果喂给 tracker 让旧轨道按 TTL 自然老化。
        if self._blank_std > 0.0 and self._frame_std(crop) < self._blank_std:
            self.stats.blank += 1
            self._log_blank()
            self.tracker.update([], frame_ts)
            return frame, []

        # 先收集这一帧所有合格的脸，再一次性提特征：新管线可以按批喂 TensorRT
        # （一张脸一次前向在板上是浪费），旧识别器则自动退化成逐张。
        dets = [det for det in self.detector.detect(crop)
                if det.size >= self._min_face_px]
        dets = self._dedupe_detections(dets)
        embeddings = self._embeddings(crop, dets)

        detections = []
        faces = []
        for det, embedding in zip(dets, embeddings):
            full = det.shifted(cx, cy)
            face_id, name, score = self._match(embedding)
            detections.append({"detection": full, "face_id": face_id, "name": name,
                               "score": score})
            faces.append({
                "bbox": full.bbox,
                "face_id": face_id,
                "name": name,
                "score": score,
                # 关联层要的证据：特征 + 质量。tracker 只认 bbox/face_id/name/score，
                # 多出的键它不看；轨道匹配完它会把 track_id 回填进来。
                "embedding": embedding,
                "rms_norm": self._quality_of(det),
            })

        events_this_frame = self.tracker.update(faces, frame_ts)
        # 本帧每条轨道**这一帧**的身份标签：要现算，不能用事件的标签 ——
        # 陌生人的事件标签是 tracker 给的轨道稳定 id（unk_0…），而 face["face_id"] 是 None
        frame_labels = dict((int(face["track_id"]), face.get("face_id") or "?")
                            for face in faces if face.get("track_id") is not None)
        self._frame_labels = frame_labels
        # 每帧都喂证据（不是只在发事件时喂）：参考库要靠整个在场的多帧建立，
        # 只在确认那一帧喂一次，质量不好的那一次会让会话永远匹配不上。
        tracked_ids = set(face.get("track_id") for face in faces
                          if face.get("track_id") is not None)
        assoc_results = {}
        if self.associator.enabled:
            self._track_good_quality(faces, frame_ts)
            for face in faces:
                track_id = face.get("track_id")
                if track_id is None:
                    continue
                assoc_results[track_id] = self.associator.observe(
                    track_id, face.get("embedding"), face.get("rms_norm"), frame_ts,
                    tracked_ids=tracked_ids)

        for event in events_this_frame:
            if not self._should_report(event, assoc_results):
                # `_should_report` 返回"压掉"有两种：关联层判重 / 首报挂起等待。
                # 挂起期间被压是延迟，不是关联层去重，计数也要分开，现场才看得懂。
                if int(event.track_id) not in self._pending:
                    self.stats.suppressed += 1
                continue
            self._report(event, frame)

        # 挂起中又把这条轨道**并回一条已报过的会话**：它是同一个人，不是新访客。
        # （出画那一两帧的残影会先把轨道绑定到自己的幽灵会话；人还在，后面几帧
        # 证据一硬就并回原会话 —— 原来只在"发事件那一帧"检查，挂起期间漏掉了。）
        # 每帧都查：两人分头出现时，事件帧少得可怜。
        self._absorb_pending(assoc_results)

        # 收割首报挂起 → 本帧"造出挂起"的轨道已经在 `_first_report` 里算过第 1 帧了，
        # 用 `skip_tracks` 把它们跳过，别在同一帧里数两遍（那会让 K 帧少算一帧）。
        if self.associator.enabled and self._defer_frames > 0:
            self._settle_pending(frame_labels, assoc_results, frame_ts, frame,
                                 self._emitted_this_frame)
        self._emitted_this_frame = set()
        return frame, detections

    def _absorb_pending(self, assoc_results):
        """这条轨道已并回一条报过的会话，就把它挂起的首报撤掉（它不是新访客）。"""
        for track_id in list(self._pending.keys()):
            if track_id not in assoc_results:
                continue
            pending = self._pending[track_id]
            result = assoc_results[track_id]
            if not result.associated or result.session_id is None:
                continue
            if result.session_id == pending["session"]:
                continue                        # 还是自己那条候选，等它自己撑够
            if result.session_id not in self._reported_sessions:
                continue                        # 并回的是还没报过的候选：不动它
            self._pending.pop(track_id, None)
            self.log("[vision] 首报挂起撤销：轨道 %s 并回已报过的会话 %s（理由 %s）→ 不是新访客"
                     % (track_id, result.session_id, result.reason))

    def _pending_still_ok(self, pending, label, result, frame_existing):
        """挂起的候选这一帧还站着吗：还是同一身份标签，而且**还没被推翻**。

        两种状态：
        - 候选还站着（本帧沿轨 track_bound，或仍分给它同一个会话）：按帧数确认；
        - 已经被推翻（本帧像是另一条会话、或低于 tau、或跟第二像的拉不开）：
          **不是立刻作废**，而是立刻结算 —— 撞了别人的会话就等于"我错了"：
          并回那条已报过的会话就不是新访客（`_settle_overturned`），
          建不出新会话就是看不清的残影（丢弃）。
          原来在这里直接丢掉挂起，结果残影轨道后面绑到别的会话也没人看，
          时间兜底到了照样报一条（2026-11-09 sc01 实测）。
        """
        if label != pending["label"]:
            return False                      # 同一轨道换了身份：直接作废
        if result is None:
            return True                       # 缺乏证据：不确认，也不推翻
        if result.reason == "track_bound":
            return True
        if result.session_id == pending["session"]:
            return True                       # 还分在自己名下（new_session / associated）
        return False

    def _settle_pending(self, labels, assoc_results, frame_ts, frame, skip_tracks=()):
        """收割首报挂起：撑够 K 帧的补发、撑够 K 毫秒的补发、被推翻的丢掉。

        `labels`：track_id -> 这一帧该轨道的身份标签（由 `_process_frame` 按帧内
        `face["face_id"]` 现算，不取 tracker 事件的标签）。
        `skip_tracks`：本帧在 `_first_report` 里刚挂起的轨道（第 1 帧算过了，
        这里跳过，同一帧数两遍会让 K 帧少算一帧）。

        计数按**帧**算：一条轨道停下来就是十几帧，两帧、五帧都撑不过来的残影
        在这里自然被筛掉；同一帧里出现第二张脸时不计数（那是别人的证据）。
        """
        self._expire_pending(frame_ts)
        crowded = len(labels) > 1
        for track_id in list(self._pending.keys()):
            pending = self._pending[track_id]
            label = labels.get(track_id)
            if label is None or track_id in skip_tracks:
                continue                      # 这一帧没检出它 / 本帧已算过
            result = assoc_results.get(track_id)
            if not self._pending_still_ok(pending, label, result, crowded):
                # 候选被推翻：立刻结算（撞了别人的会话就并回去，或者当作看不清而丢弃）
                self._settle_overturned(track_id, pending, result, frame)
                continue
            if crowded:
                continue                      # 场上还有别人：不借别人的在场给别人续命
            pending["count"] += 1
            elapsed = int(frame_ts) - pending["first_ts"]
            if pending["count"] >= pending["need"] or elapsed >= self._defer_ms:
                self._pending.pop(track_id, None)
                self.log("[vision] 首报确认（轨道 %s：%d 帧 / %d ms，理由 %s）后才上报："
                         "现挂一下就走的不算访客"
                         % (track_id, pending["count"], elapsed,
                            pending["result"].reason if pending["result"] is not None
                            else "no_session"))
                self._commit_report(pending["event"], pending["session"])
                self._report(pending["event"], frame)

    def _settle_overturned(self, track_id, pending, result, frame):
        """挂起的候选被这一帧推翻了：并回已报过的会话，或者判断它是看不清的残影。"""
        self._pending.pop(track_id, None)
        session_id = None if result is None else result.session_id
        if (session_id is not None and result.associated
                and session_id in self._reported_sessions):
            self.log("[vision] 首报挂起撤销：轨道 %s 并回已报过的会话 %s → 不是新访客"
                     % (track_id, session_id))
            return
        # 建不出新会话（开着的会话都被占着 / 看不出像谁）：正是出画那一两帧的残影形态
        self.stats.deferred += 1
        self.associator.drop_session(pending["session"])
        self.log("[vision] 首报候选被推翻（轨道 %s：理由 %s own=%s other=%s）→ 不报"
                 % (track_id,
                    None if result is None else result.reason,
                    "%.2f" % result.own_best if result is not None
                    and result.own_best is not None else "—",
                    "%.2f" % result.other_best if result is not None
                    and result.other_best is not None else "—"))

    @staticmethod
    def _frame_std(frame, probe_width=BLANK_PROBE_WIDTH):
        """缩小后的灰度标准差：越小画面越"均匀"（全黑 / 镜头被遮 / 流掉线）。

        先缩小再算，720p 上约 0.2 ms；全黑与暗帧(16) 都是 0，真实素材帧 20+。
        """
        height, width = frame.shape[:2]
        if height <= 0 or width <= 0:
            return 0.0
        if width > probe_width:
            scale = float(probe_width) / float(width)
            frame = cv2.resize(frame, (int(probe_width), max(1, int(height * scale))),
                               interpolation=cv2.INTER_AREA)
        gray = frame if frame.ndim == 2 else cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
        _mean, std = cv2.meanStdDev(gray)
        return float(std[0][0])

    def _log_blank(self):
        """限频：全黑环境 17 fps 会刷屏，所以只打前 3 条 + 每 100 条一次。"""
        self._blank_logged += 1
        if self._blank_logged <= 3 or self._blank_logged % 100 == 0:
            self.log("[vision] 空白帧保护：画面近均匀，跳过检测（累计 %d 帧）"
                     % self.stats.blank)

    def _embeddings(self, crop, dets):
        """一帧一次批量识别：有 `embed_batch` 就用，没有就逐张调 `embed`。

        批量返回长度不对时不硬凑（那会把特征和脸对错位），退回逐张并在日志里说明。
        """
        if not dets:
            return []
        batch = getattr(self.recognizer, "embed_batch", None)
        if batch is not None:
            vectors = list(batch(crop, dets))
            if len(vectors) == len(dets):
                return vectors
            self.log("[vision] 识别器批量返回 %d 个向量、本帧 %d 张脸，退回逐张"
                     % (len(vectors), len(dets)))
        return [self.recognizer.embed(crop, det) for det in dets]

    def _match(self, embedding):
        """`(face_id, name, score)`；陌生人 face_id/name 为 None。

        陌生人也带 score（与库里最像的那个人的相似度）—— 现场调阈值时
        「差一点就认出来」和「完全不像」是两回事，这个数字必须留着。
        """
        if embedding is None:
            return (None, None, 0.0)
        face_id, name, score = self.gallery.best(embedding)
        if face_id is not None and score >= self._match_threshold:
            return (face_id, name, float(score))
        return (None, None, float(score))

    def _report(self, event, frame):
        msg = events.face_event(
            face_id=event.face_id, is_known=event.is_known, name=event.name,
            confidence=event.confidence, bbox=event.bbox, frame_ts=event.frame_ts,
        )
        problems = events.check(msg)
        if problems:
            self.log("[vision] 事件不合规，仍发送但请修：%s" % "；".join(problems))

        snapshot_path = self._save_snapshot(event, frame)
        self._emit(msg)
        self.stats.events += 1
        if self._print_events:
            tag = event.name if event.is_known else "陌生人"
            self.log("[vision] 上报 %s · %s · conf=%.3f · %s%s" % (
                event.face_id, tag, event.confidence,
                "截图 %s" % snapshot_path if snapshot_path else "无截图",
                "" if self.stats.failed == 0 else "（累计发送失败 %d）" % self.stats.failed,
            ))

    def _emit(self, msg):
        if self.sink.send(msg):
            self.stats.sent += 1
        else:
            self.stats.failed += 1
            if msg.get("type") == "face_event":
                self.log("[vision] 事件发送失败（对端没起？），本地计数 = %d" % self.stats.failed)

    # ---------------------------------------------------------------- 截图

    def _save_snapshot(self, event, frame):
        if not self._save_snapshots:
            return None
        day = time.strftime("%Y%m%d", time.localtime(event.frame_ts / 1000.0))
        out_dir = os.path.join(self._snapshot_dir, day)
        os.makedirs(out_dir, exist_ok=True)
        name = "%s_%d.jpg" % (event.face_id, event.frame_ts)
        path = os.path.join(out_dir, name)
        canvas = frame.copy()
        x, y, w, h = event.bbox
        color = (0, 200, 0) if event.is_known else (0, 165, 255)
        cv2.rectangle(canvas, (x, y), (x + w, y + h), color, 2)
        label = ("%s %.2f" % (event.face_id, event.confidence)).encode("ascii", "replace").decode("ascii")
        cv2.putText(canvas, label, (x, max(14, y - 6)),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.5, color, 1, cv2.LINE_AA)
        imwrite(path, canvas, jpeg_quality=self._snapshot_quality)
        self._snapshot_saved += 1
        return os.path.relpath(path, os.path.dirname(self._snapshot_dir))

    # ---------------------------------------------------------------- 杂项

    def _default_log(self, message):
        print("%s %s" % (time.strftime("%H:%M:%S"), message))


def build_sink(cfg, jsonl_path=None, log=None):
    """按配置造出口。

    `jsonl_path` 是**附加**出口（tee），不是替换 —— 联调时既要看终端也要留档，
    如果让它顶掉 WS，"事件有没有发出去"这件事就被掩盖了（2026-10-23 踩过）。
    """
    sinks = []
    if cfg["server"]["enabled"]:
        sinks.append(WsSink(cfg["server"]["ws_url"], cfg["server"]["reconnect_sec"],
                            timeout=cfg["server"].get("timeout", 1.0),
                            send_timeout=cfg["server"].get("send_timeout", 0.5),
                            max_reconnect_sec=cfg["server"].get("max_reconnect_sec", 60.0),
                            log=log))
    if jsonl_path:
        sinks.append(JsonlSink(jsonl_path, log=log))
    if not sinks:
        return NullSink()
    if len(sinks) == 1:
        return sinks[0]
    return MultiSink(sinks)
