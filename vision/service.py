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
        self.blank = 0          # 帧级均匀保护跳过的近均匀帧（镜头被遮/全黑/流掉线）
        self.fps = 0.0
        self.infer_ms = 0.0
        self.started_at = time.time()

    def summary(self):
        elapsed = max(1e-6, time.time() - self.started_at)
        blank = " · 空白帧 %d" % self.blank if self.blank else ""
        return ("帧 %d（%.1f fps）· 上报 %d 条（成功 %d / 失败 %d）· "
                "单帧推理 %.1f ms · 丢帧 %d · 关联去重 %d%s" % (
                    self.frames, self.frames / elapsed, self.events,
                    self.sent, self.failed, self.infer_ms, self.dropped,
                    self.suppressed, blank,
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

    def _should_report(self, event, assoc_results):
        """未知人脸过一层匿名会话去重；返回 False 表示这次不该再报。"""
        if event.is_known or not self.associator.enabled:
            return True
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
            return False
        self._reported_sessions.add(result.session_id)
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
        # 每帧都喂证据（不是只在发事件时喂）：参考库要靠整个在场的多帧建立，
        # 只在确认那一帧喂一次，质量不好的那一次会让会话永远匹配不上。
        assoc_results = {}
        if self.associator.enabled:
            tracked_ids = set(face.get("track_id") for face in faces
                              if face.get("track_id") is not None)
            for face in faces:
                track_id = face.get("track_id")
                if track_id is None:
                    continue
                assoc_results[track_id] = self.associator.observe(
                    track_id, face.get("embedding"), face.get("rms_norm"), frame_ts,
                    tracked_ids=tracked_ids)

        for event in events_this_frame:
            if not self._should_report(event, assoc_results):
                self.stats.suppressed += 1
                continue
            self._report(event, frame)

        return frame, detections

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
