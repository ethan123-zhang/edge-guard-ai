"""视觉主循环：读帧 → 检测 → 识别 → 稳定 → 上报（+ 心跳 + 截图）。

`Sink` 是唯一的出口：`WsSink` 发给对话节点，`JsonlSink` 落文件（联调用），
`NullSink` 什么都不做（压测/单测）。这样主循环本身完全不依赖网络。
"""
import json
import os
import time

import cv2

from vision import config, events
from vision.imageio import imwrite
from vision.ws_client import WebSocketClient, WsClientError


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

    def __init__(self, url, reconnect_sec=3.0, log=None):
        self._client = WebSocketClient(url, reconnect_sec=reconnect_sec, log=log)
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
        self.fps = 0.0
        self.infer_ms = 0.0
        self.started_at = time.time()

    def summary(self):
        elapsed = max(1e-6, time.time() - self.started_at)
        return ("帧 %d（%.1f fps）· 上报 %d 条（成功 %d / 失败 %d）· "
                "单帧推理 %.1f ms · 丢帧 %d" % (
                    self.frames, self.frames / elapsed, self.events,
                    self.sent, self.failed, self.infer_ms, self.dropped,
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
        self._save_snapshots = bool(cfg["output"]["save_snapshots"])
        self._snapshot_dir = config.resolve_project(cfg["output"]["snapshot_dir"])
        self._snapshot_quality = int(cfg["output"]["snapshot_quality"])
        self._print_events = bool(cfg["output"]["print_events"])
        self._roi = self._roi_rect_from_cfg(cfg["roi"])
        self._snapshot_saved = 0

    # ---------------------------------------------------------------- 配置派生

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
        self.log("[vision] 人脸库：%d 人" % len(self.gallery))
        self.log("[vision] 事件出口：%s" % getattr(self.sink, "name", "?"))

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
                    self._emit(events.heartbeat(self.stats.fps, self.stats.dropped))

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

    def _process_frame(self, frame, frame_ts):
        height, width = frame.shape[:2]
        cx, cy, cw, ch = self._crop_rect(frame.shape)
        crop = frame[cy:cy + ch, cx:cx + cw]

        detections = []
        faces = []
        for det in self.detector.detect(crop):
            if det.size < self._min_face_px:
                continue
            full = det.shifted(cx, cy)
            face_id, name, score = self._identify(crop, det)
            detections.append({"detection": full, "face_id": face_id, "name": name,
                               "score": score})
            faces.append({
                "bbox": full.bbox,
                "face_id": face_id,
                "name": name,
                "score": score,
            })

        for event in self.tracker.update(faces, frame_ts):
            self._report(event, frame)

        return frame, detections

    def _identify(self, crop, det):
        """返回 `(face_id, name, score)`；陌生人 face_id/name 为 None。

        陌生人也带 score（与库里最像的那个人的相似度）—— 现场调阈值时
        「差一点就认出来」和「完全不像」是两回事，这个数字必须留着。
        """
        embedding = self.recognizer.embed(crop, det)
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
        sinks.append(WsSink(cfg["server"]["ws_url"], cfg["server"]["reconnect_sec"], log=log))
    if jsonl_path:
        sinks.append(JsonlSink(jsonl_path, log=log))
    if not sinks:
        return NullSink()
    if len(sinks) == 1:
        return sinks[0]
    return MultiSink(sinks)
