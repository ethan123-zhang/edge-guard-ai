"""PC 仿真入口：摄像头 → 检测 → 识别 → face_event → 对话节点。

用法（在仓库根目录）：
    python scripts\\vision_sim.py                     # sim 档，开预览窗口
    python scripts\\vision_sim.py --no-window          # 无窗口（我这边的自动化测试这样跑）
    python scripts\\vision_sim.py --duration 20        # 跑 20 秒自动退出
    python scripts\\vision_sim.py --source 1           # 换摄像头序号
    python scripts\\vision_sim.py --source demo\\imgs   # 图片目录轮播
    python scripts\\vision_sim.py --jsonl logs\\events.jsonl   # 同时落文件，方便复盘
    python scripts\\vision_sim.py --demo               # 回放预置事件流（演示兜底）

按键：q / ESC 退出，s 存一张当前画面。
"""
import argparse
import json
import os
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

import cv2

from vision import camera, config, detector, events, gallery, recognizer, service, tracker
from vision.imageio import imwrite


def log(message):
    print("%s %s" % (time.strftime("%H:%M:%S"), message))


def parse_args(argv=None):
    ap = argparse.ArgumentParser(description="哨兵管家 · 视觉节点 PC 仿真")
    ap.add_argument("--profile", default="sim", help="sim / board（vision/config/*.yaml）")
    ap.add_argument("--config-dir", default=None, help="配置目录（默认 vision/config）")
    ap.add_argument("--source", default=None, help="覆盖 camera.source：序号 / 视频 / 图片目录")
    ap.add_argument("--no-window", action="store_true", help="不开预览窗口（SSH / 自动化）")
    ap.add_argument("--no-server", action="store_true", help="不连对话节点，只本地打印")
    ap.add_argument("--duration", type=float, default=None, help="跑多少秒后退出（0=不限）")
    ap.add_argument("--jsonl", default=None, help="事件另存 JSONL（调试用）")
    ap.add_argument("--min-face-px", type=int, default=None,
                    help="覆盖 detection.min_face_px（小图测试时调小，例如 30）")
    ap.add_argument("--demo", action="store_true", help="回放预置事件流，不读摄像头")
    return ap.parse_args(argv)


def apply_overrides(cfg, args):
    if args.source is not None:
        spec = args.source
        cfg["camera"]["source"] = int(spec) if spec.isdigit() else spec
    if args.no_server:
        cfg["server"]["enabled"] = False
    if args.no_window:
        cfg["runtime"]["headless"] = True
    if args.duration is not None:
        cfg["runtime"]["duration_sec"] = float(args.duration)
    if args.min_face_px is not None:
        cfg["detection"]["min_face_px"] = int(args.min_face_px)
    return cfg


def build_pipeline(cfg):
    det = detector.create_detector(cfg, log=log)
    try:
        rec = recognizer.create_recognizer(cfg, log=log)
    except recognizer.RecognizerUnavailable as exc:
        log("[vision] 识别器不可用，降级为 none：%s" % exc)
        rec = recognizer.NullRecognizer()

    gallery_dir = config.resolve_vision(cfg["recognition"]["gallery_dir"])
    store = gallery.FaceGallery(gallery_dir, log=log).load()
    track = tracker.FaceTracker(
        confirm_frames=cfg["recognition"]["confirm_frames"],
        cooldown_sec=cfg["recognition"]["cooldown_sec"],
        track_ttl_sec=cfg["recognition"]["track_ttl_sec"],
    )
    return det, rec, store, track


def annotate(frame, detections, stats, cfg):
    """画面标注：ROI 框 + 每个人脸框 + 左上角统计。中文用不上 cv2.putText，标签用 ASCII。"""
    canvas = frame.copy()
    height, width = canvas.shape[:2]
    if cfg["roi"]["enabled"]:
        roi = cfg["roi"]
        p1 = (int(roi["x"] * width), int(roi["y"] * height))
        p2 = (int((roi["x"] + roi["w"]) * width), int((roi["y"] + roi["h"]) * height))
        cv2.rectangle(canvas, p1, p2, (200, 200, 200), 1)

    for item in detections:
        face = item["detection"]
        x, y, w, h = face.bbox
        known = item["face_id"] is not None
        color = (0, 200, 0) if known else (0, 165, 255)
        cv2.rectangle(canvas, (x, y), (x + w, y + h), color, 2)
        label = ("%s %.2f" % (item["face_id"], item["score"]) if known
                 else "unknown %.2f" % item["score"])
        cv2.putText(canvas, label, (x, max(14, y - 8)),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.5, color, 1, cv2.LINE_AA)

    hud = "%s  %.1ffps  %.1fms  ev=%d" % (
        os.path.basename(str(cfg["camera"]["source"])), stats.fps, stats.infer_ms, stats.events)
    cv2.putText(canvas, hud, (10, 22), cv2.FONT_HERSHEY_SIMPLEX, 0.55,
                (255, 255, 255), 1, cv2.LINE_AA)
    return canvas


def run_pipeline(cfg, args):
    source = camera.create_source(cfg)
    det, rec, store, track = build_pipeline(cfg)
    sink = service.build_sink(cfg, jsonl_path=args.jsonl, log=log)
    svc = service.VisionService(cfg, source, det, rec, store, track, sink, log=log)

    show_window = (not cfg["runtime"]["headless"]) and not args.no_window
    state = {"stop": False, "grabbed": 0}

    log("[vision] profile=%s · 摄像头=%s" % (cfg["profile"], cfg["camera"]["source"]))
    log("[vision] 提示：对着摄像头站 2~3 秒；按 q 退出，s 截图")

    def on_frame(frame, detections, stats):
        canvas = annotate(frame, detections, stats, cfg)
        if not show_window:
            return
        cv2.imshow("sentinel-vision-sim", canvas)
        key = cv2.waitKey(1) & 0xFF
        if key in (ord("q"), 27):
            state["stop"] = True
        elif key == ord("s"):
            state["grabbed"] += 1
            path = os.path.join(ROOT, "snapshots", "manual_%d.jpg" % int(time.time()))
            os.makedirs(os.path.dirname(path), exist_ok=True)
            imwrite(path, canvas)
            log("[vision] 已截图：%s" % path)

    started = time.time()
    try:
        stats = svc.run(on_frame=on_frame, should_stop=lambda: state["stop"])
    except KeyboardInterrupt:
        log("[vision] 收到 Ctrl-C，收尾")
        stats = svc.stats
    finally:
        if show_window:
            cv2.destroyAllWindows()

    log("[vision] 结束：%s（共 %.1f 秒）" % (stats.summary(), time.time() - started))
    if svc.stats.failed:
        log("[vision] 有 %d 条事件没发出去：检查假对话节点是否在 %s"
            % (svc.stats.failed, cfg["server"]["ws_url"]))
    return 0


def run_demo(cfg, args):
    """演示兜底：不依赖摄像头，按预置时间线把事件流打给对话节点。"""
    path = os.path.join(ROOT, "vision", "demo_events.jsonl")
    if not os.path.exists(path):
        log("[demo] 缺少预置事件文件：%s" % path)
        return 2
    sink = service.build_sink(cfg, jsonl_path=args.jsonl, log=log)
    log("[demo] 回放 %s → %s" % (path, cfg["server"]["ws_url"] if cfg["server"]["enabled"] else "本地"))
    with open(path, "r", encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            try:
                # 只做 JSON 透传：只刷新 ts，保留预置的 face_id / bbox，便于演示叙事
                payload = json.loads(line)
                payload["ts"] = events.now_ms()
                sink.send(payload)
            except ValueError:
                continue
            log("[demo] 已发：%s" % line[:110])
            time.sleep(2.5)
    sink.close()
    return 0


def main(argv=None):
    args = parse_args(argv)
    cfg = config.load(args.profile, args.config_dir)
    cfg = apply_overrides(cfg, args)
    config.validate(cfg)
    try:
        if args.demo:
            return run_demo(cfg, args)
        return run_pipeline(cfg, args)
    except (camera.SourceError, detector.DetectorUnavailable) as exc:
        log("[vision] 启动失败：%s" % exc)
        return 2


if __name__ == "__main__":
    sys.exit(main())
