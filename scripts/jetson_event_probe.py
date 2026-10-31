#!/usr/bin/env python3
"""JETSON-EVENT-DIAG-03：同帧比较探针 Haar 与生产 Haar 的检测/跟踪差异。

只读诊断：不识别身份、不加载人脸库、不保存图像、不联网、不启动服务。
用法：python3 scripts/jetson_event_probe.py --json logs/jetson_event_probe.json
"""
import argparse, json, os, sys, time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

import cv2
from vision import camera, config, detector, events, tracker

DEFAULT_HAAR = "/usr/share/opencv4/haarcascades/haarcascade_frontalface_default.xml"
class ProbeError(RuntimeError): pass

def positive_int(value):
    try:
        number = int(value)
    except (TypeError, ValueError):
        raise argparse.ArgumentTypeError("必须是正整数：%r" % value)
    if number <= 0:
        raise argparse.ArgumentTypeError("必须大于 0：%r" % value)
    return number

def parse_args(argv=None):
    parser = argparse.ArgumentParser(
        description="JETSON-EVENT-DIAG-03：探针/生产 Haar + FaceTracker 同帧诊断")
    parser.add_argument("--frames", type=positive_int, default=40, help="有效帧数")
    parser.add_argument("--width", type=positive_int, default=640, help="请求宽度")
    parser.add_argument("--height", type=positive_int, default=480, help="请求高度")
    parser.add_argument("--json", dest="json_path", required=True,
                        help="JSON 输出路径（父目录必须已存在）")
    return parser.parse_args(argv)

def resolve_cascade(cfg):
    candidates = [str(cfg["detection"].get("haar_cascade") or ""), DEFAULT_HAAR]
    data = getattr(cv2, "data", None)
    if data is not None:
        candidates.append(os.path.join(
            getattr(data, "haarcascades", ""), "haarcascade_frontalface_default.xml"))
    for path in candidates:
        if path and os.path.isfile(path):
            return path
    raise ProbeError("找不到 Haar XML（试过：%s）" % "；".join(p for p in candidates if p))
def check_json_path(path):
    parent = os.path.dirname(os.path.abspath(path))
    if not os.path.isdir(parent):
        raise ProbeError("JSON 父目录不存在：%s" % parent)
    return os.path.abspath(path)
def boxes(detections):
    return [{"bbox": list(d.bbox), "size": int(d.size)} for d in detections]
def tracker_state(face_tracker):
    """只读 _tracks，给单帧诊断留快照。"""
    return [{"id": int(t.id), "votes": dict(t.votes), "last_ts": int(t.last_ts),
             "emitted": bool(t.emitted)} for t in face_tracker._tracks]
def unknown_faces(detections):
    return [{"bbox": d.bbox, "face_id": None, "name": None, "score": 0.0} for d in detections]
def probe_detect(cascade, frame):
    """复现已成功的探针：灰度、不 equalize、minSize 30。"""
    gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
    found = cascade.detectMultiScale(
        gray, scaleFactor=1.1, minNeighbors=5, minSize=(30, 30))
    if found is None: found = ()
    return [detector.FaceDetection(x, y, w, h, 0.0) for (x, y, w, h) in found]
def event_logs(track_events, frame_ts):
    logs = []
    for event in track_events:
        msg = events.face_event(event.face_id, event.is_known, event.name,
                                event.confidence, event.bbox, event.frame_ts, frame_ts)
        logs.append({"track_id": event.track_id, "event": msg, "problems": events.check(msg)})
    return logs
def new_tracker(cfg):
    rec = cfg["recognition"]
    return tracker.FaceTracker(rec["confirm_frames"], rec["cooldown_sec"], rec["track_ttl_sec"])
def write_json(path, payload):
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(payload, fh, ensure_ascii=False, indent=2); fh.write("\n")

def run(args):
    cfg = config.load("jetson_local")
    cfg["camera"].update({"source": 0, "backend": "v4l2", "width": args.width,
                          "height": args.height, "warmup_frames": 5})
    config.validate(cfg)
    json_path = check_json_path(args.json_path)
    cascade_path = resolve_cascade(cfg)
    cfg["detection"]["haar_cascade"] = cascade_path
    cascade = cv2.CascadeClassifier(cascade_path)
    if cascade.empty(): raise ProbeError("Haar 级联加载失败：%s" % cascade_path)
    production = detector.create_detector(cfg)
    min_face_px = int(cfg["detection"]["min_face_px"])
    max_fps = float(cfg["runtime"]["max_fps"])
    frame_interval = (1.0 / max_fps) if max_fps > 0 else 0.0
    probe_tracker, production_tracker = new_tracker(cfg), new_tracker(cfg)
    tracker_cfg = {key: cfg["recognition"][key] for key in (
        "confirm_frames", "cooldown_sec", "track_ttl_sec")}
    payload = {
        "ok": False,
        "config": {"profile": cfg["profile"], "frames": args.frames,
                   "width": args.width, "height": args.height,
                   "camera": dict(cfg["camera"]), "detection": dict(cfg["detection"]),
                   "tracker": tracker_cfg, "max_fps": max_fps,
                   "frame_interval_sec": frame_interval},
        "detectors": {
            "probe": {"backend": "haar", "cascade": cascade_path, "equalize": False,
                      "scale_factor": 1.1, "min_neighbors": 5, "min_size": 30},
            "production": {"backend": production.backend,
                           "describe": production.describe(),
                           "equalize": production.backend == "haar",
                           "min_face_px": min_face_px}},
        "actual_frame_size": None, "frames": [], "frames_valid": 0,
        "frames_failed": 0, "total_events": 0, "probe_total_events": 0,
        "elapsed_sec": 0.0}
    cam = camera.OpenCVCamera(
        device=0, width=args.width, height=args.height, backend="v4l2",
        mirror=bool(cfg["camera"]["mirror"]), warmup_frames=5)
    started = time.perf_counter()
    consecutive_failures = 0
    try:
        cam.open()
        while payload["frames_valid"] < args.frames:
            loop_started = time.perf_counter()
            try:
                ok, frame, frame_ts = cam.read()
            except Exception:
                ok, frame, frame_ts = False, None, None
            if not ok or frame is None:
                payload["frames_failed"] += 1
                consecutive_failures += 1
                if consecutive_failures >= 50: break
                time.sleep(0.05)
                continue
            consecutive_failures = 0
            frame_no = payload["frames_valid"] + 1
            process_started = time.perf_counter()
            try:
                probe_before = tracker_state(probe_tracker)
                production_before = tracker_state(production_tracker)
                probe_detections = probe_detect(cascade, frame)
                production_raw = production.detect(frame)
                production_kept = [det for det in production_raw if det.size >= min_face_px]
                probe_events = probe_tracker.update(unknown_faces(probe_detections), frame_ts)
                production_events = production_tracker.update(
                    unknown_faces(production_kept), frame_ts)
                probe_logs = event_logs(probe_events, frame_ts)
                production_logs = event_logs(production_events, frame_ts)
            except Exception as exc:
                raise ProbeError("第 %d 帧检测/跟踪异常：%s: %s" % (
                    frame_no, exc.__class__.__name__, exc))
            process_ms = (time.perf_counter() - process_started) * 1000.0
            if payload["actual_frame_size"] is None:
                payload["actual_frame_size"] = [int(frame.shape[1]), int(frame.shape[0])]
            payload["frames_valid"] = frame_no
            payload["total_events"] += len(production_logs)
            payload["probe_total_events"] += len(probe_logs)
            probe_line = {"count": len(probe_detections), "boxes": boxes(probe_detections),
                          "tracker_before": probe_before, "events": probe_logs,
                          "event_count": len(probe_logs),
                          "tracker_after": tracker_state(probe_tracker)}
            production_line = {
                "count_before": len(production_raw), "count_after": len(production_kept),
                "filtered_out": len(production_raw) - len(production_kept),
                "boxes_before": boxes(production_raw), "boxes": boxes(production_kept),
                "tracker_before": production_before, "events": production_logs,
                "event_count": len(production_logs),
                "tracker_after": tracker_state(production_tracker)}
            payload["frames"].append({"index": frame_no, "frame_ts": int(frame_ts),
                                      "process_ms": round(process_ms, 2),
                                      "probe": probe_line, "production": production_line})
            if frame_interval > 0:
                spare = frame_interval - (time.perf_counter() - loop_started)
                if spare > 0: time.sleep(spare)
    finally:
        cam.release()
    payload["elapsed_sec"] = round(time.perf_counter() - started, 3)
    if payload["frames_valid"] == 0:
        payload["error"] = "没有有效帧（失败 %d 次）" % payload["frames_failed"]
        write_json(json_path, payload)
        raise ProbeError("%s；摄像头打不开或被占用" % payload["error"])
    payload["ok"] = True
    write_json(json_path, payload)
    return payload, json_path

def main(argv=None):
    args = parse_args(argv)
    try:
        payload, json_path = run(args)
    except (ProbeError, camera.SourceError, detector.DetectorUnavailable,
            cv2.error, OSError, ValueError) as exc:
        print("[probe] 失败：%s" % exc, file=sys.stderr)
        return 2
    print("[probe] 有效帧 %d / 失败帧 %d / 生产事件 %d / 探针事件 %d / %.2fs -> %s" % (
        payload["frames_valid"], payload["frames_failed"], payload["total_events"],
        payload["probe_total_events"], payload["elapsed_sec"], json_path))
    return 0

if __name__ == "__main__":
    sys.exit(main())
