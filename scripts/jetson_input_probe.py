#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""JETSON-INPUT-PROBE-01：同帧输入取证（只读）。

同一帧：灰度统计 + 四路 Haar 对照（plain30/plain80/equalized30/equalized80，
scaleFactor=1.1、minNeighbors=5）+ 生产 detector.detect 与
suppress_nested_detections(配置实际选择的 raw 路径：equalized80 或 plain80 框) 同帧校验。
30px 仅诊断，不改生产门槛；
不保存/编码图像、不联网、不识别身份。零框不是错误，退出 0 只代表采样完整。
用法：python3 scripts/jetson_input_probe.py --json logs/jetson_input_probe.json
"""
from __future__ import print_function

import argparse
import hashlib
import json
import math
import os
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

TASK = "JETSON-INPUT-PROBE-01"
PROFILE = "jetson_local"
DEV, W, H, BACKEND = 0, 1280, 720, "v4l2"
PATHS = (("plain30", False, 30), ("plain80", False, 80),
         ("equalized30", True, 30), ("equalized80", True, 80))
DEFAULT_HAAR = "/usr/share/opencv4/haarcascades/haarcascade_frontalface_default.xml"
EXIT_OK, EXIT_FAIL = 0, 2


class ProbeError(RuntimeError):
    pass


def positive(kind, value):
    try:
        number = kind(value)
    except (TypeError, ValueError):
        raise argparse.ArgumentTypeError("必须是正数：%r" % value)
    if isinstance(number, float):
        if not math.isfinite(number):
            raise argparse.ArgumentTypeError("必须是有限正数（拒绝 nan/inf）：%r" % value)
        if number <= 0.0:
            raise argparse.ArgumentTypeError("必须大于 0：%r" % value)
        return number
    if number <= 0:
        raise argparse.ArgumentTypeError("必须大于 0：%r" % value)
    return number


def parse_args(argv=None):
    parser = argparse.ArgumentParser(description="JETSON-INPUT-PROBE-01：同帧灰度/四路 Haar/生产检测对照（只读）")
    parser.add_argument("--json", dest="json_path", required=True,
                        help="JSON 路径（父目录必须已存在）")
    parser.add_argument("--frames", type=lambda v: positive(int, v), default=20,
                        help="采样帧数上限")
    parser.add_argument("--period", type=lambda v: positive(float, v), default=1.0,
                        help="帧间隔秒数（有限正数）")
    return parser.parse_args(argv)


def exception_text(exc):
    try:
        text = " ".join(str(exc).split())
    except Exception:
        text = repr(exc)
    return text if len(text) <= 800 else text[:797] + "..."


def err(payload, stage, exc=None, frame=None, path=None, message=None):
    payload["errors"].append({
        "stage": stage,
        "path": path,
        "frame_index": frame,
        "type": exc.__class__.__name__ if exc is not None else "ProbeError",
        "message": message if message is not None else (
            exception_text(exc) if exc is not None else "未知错误"),
    })


def warn(payload, message):
    payload["warnings"].append(message)


def pcts(values):
    if not values:
        return {"p50": None, "p95": None, "samples": 0}
    ordered = sorted(values)
    last = len(ordered) - 1
    p50 = ordered[int(round(0.50 * last))]
    p95 = ordered[int(round(0.95 * last))]
    return {"p50": round(float(p50), 2), "p95": round(float(p95), 2),
            "samples": len(ordered)}


def check_json(path):
    parent = os.path.dirname(os.path.abspath(path))
    if not os.path.isdir(parent):
        raise ProbeError("JSON 父目录不存在：%s（不会自动创建）" % parent)
    return os.path.abspath(path)


def new_payload(args, json_path):
    return {
        "task": TASK,
        "timestamp_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "python": sys.version.split()[0],
        "profile": PROFILE,
        "status": "running",
        "complete": False,
        "success": False,
        "exit_code": EXIT_FAIL,
        "json_path": json_path,
        "request": {"device": DEV, "backend": BACKEND,
                    "resolution": "%dx%d" % (W, H),
                    "frames": args.frames, "period_sec": args.period},
        "camera": {"device": DEV, "backend": BACKEND, "opened": False,
                   "released": False, "warmup": None, "actual_resolution": None,
                   "valid_frames": 0, "failed_frames": 0, "props": {}},
        "paths": dict((name, {"equalized": eq, "min_size": ms, "scale_factor": 1.1,
                              "min_neighbors": 5, "measured_frames": 0,
                              "detected_frames": 0, "total_boxes": 0,
                              "failed_frames": 0, "elapsed_ms": []})
                      for name, eq, ms in PATHS),
        "production": {"backend": None, "measured_frames": 0, "detected_frames": 0,
                       "total_boxes": 0, "elapsed_ms": []},
        "comparison": {"checked_frames": 0, "matched_frames": 0,
                       "mismatched_frames": 0, "mismatch_frame_indices": [],
                       "reference_path": None},
        "frames": [], "errors": [], "warnings": [], "summary": {},
    }


PROPS = (("width", "CAP_PROP_FRAME_WIDTH", "int"),
         ("height", "CAP_PROP_FRAME_HEIGHT", "int"),
         ("fps", "CAP_PROP_FPS", "number"),
         ("fourcc", "CAP_PROP_FOURCC", "fourcc"),
         ("brightness", "CAP_PROP_BRIGHTNESS", "number"),
         ("gain", "CAP_PROP_GAIN", "number"),
         ("exposure", "CAP_PROP_EXPOSURE", "number"),
         ("auto_exposure", "CAP_PROP_AUTO_EXPOSURE", "number"))


def fourcc_text(raw):
    code = int(raw)
    if code <= 0:
        return None
    chars = []
    for shift in (0, 8, 16, 24):
        value = (code >> shift) & 0xFF
        chars.append(chr(value) if 32 <= value < 127 else "?")
    return "".join(chars)


def read_props(cv2, cam, payload):
    """只读 cam._cap.get，不改任何摄像头控制值。"""
    core = getattr(cam, "_cap", None)
    for key, prop_name, kind in PROPS:
        entry = {"get_ok": False, "value": None, "error": None}
        prop = getattr(cv2, prop_name, None)
        if prop is None:
            entry["error"] = "当前 OpenCV 没有 %s" % prop_name
        elif core is None:
            entry["error"] = "cam._cap 为 None，无法读取"
        else:
            try:
                raw = float(core.get(prop))
            except Exception as exc:
                entry["error"] = exception_text(exc)
                err(payload, "camera_get_" + key, exc)
            else:
                entry["get_ok"] = True
                if kind == "int":
                    entry["value"] = int(round(raw))
                elif kind == "fourcc":
                    entry["value"] = fourcc_text(raw)
                else:
                    entry["value"] = round(raw, 4)
        payload["camera"]["props"][key] = entry


def stats(cv2, np, gray, prev, frame_ts, shape, dtype):
    lo, mid, hi = [float(v) for v in np.percentile(gray, [1.0, 50.0, 99.0])]
    count = float(gray.shape[0] * gray.shape[1])
    diff = None
    if prev is not None and prev.shape == gray.shape:
        diff = round(float(np.abs(
            gray.astype(np.float32) - prev.astype(np.float32)).mean()), 4)
    return {
        "frame_ts_ms": None if frame_ts is None else int(frame_ts),
        "frame_shape": [int(v) for v in shape],
        "frame_dtype": str(dtype),
        "gray_mean": round(float(gray.mean()), 3),
        "gray_std": round(float(gray.std()), 3),
        "gray_p1": round(lo, 2),
        "gray_p50": round(mid, 2),
        "gray_p99": round(hi, 2),
        "dark_ratio_lt16": round(float(np.count_nonzero(gray < 16)) / count, 6),
        "bright_ratio_gt240": round(float(np.count_nonzero(gray > 240)) / count, 6),
        "laplacian_var": round(float(cv2.Laplacian(gray, cv2.CV_64F).var()), 3),
        "gray_sha256": hashlib.sha256(np.ascontiguousarray(gray).tobytes()).hexdigest(),
        "diff_prev_mean_abs": diff,
    }


def run_paths(cv2, cascade, gray, payload, index):
    results = {}
    for name, use_equalized, min_size in PATHS:
        record = {"equalized": use_equalized, "min_size": min_size, "boxes": None,
                  "count": None, "elapsed_ms": None, "error": None}
        started = time.perf_counter()
        try:
            source = cv2.equalizeHist(gray) if use_equalized else gray
            found = cascade.detectMultiScale(
                source, scaleFactor=1.1, minNeighbors=5,
                minSize=(min_size, min_size), flags=cv2.CASCADE_SCALE_IMAGE)
            record["boxes"] = ([] if found is None
                               else [[int(v) for v in box] for box in found])
            record["count"] = len(record["boxes"])
        except Exception as exc:
            record["error"] = exception_text(exc)
            record["elapsed_ms"] = round((time.perf_counter() - started) * 1000.0, 3)
            state = payload["paths"][name]
            state["failed_frames"] += 1
            state["elapsed_ms"].append(record["elapsed_ms"])
            err(payload, "path_" + name, exc, frame=index, path=name,
                message="%s 路抛错（不按 0 框计）：%s" % (name, exception_text(exc)))
            results[name] = record
            for other, other_eq, other_min in PATHS:
                if other not in results:
                    results[other] = {"equalized": other_eq, "min_size": other_min,
                                      "boxes": None, "count": None,
                                      "elapsed_ms": None,
                                      "error": "not_attempted：前一路失败已中止"}
            return results, True
        record["elapsed_ms"] = round((time.perf_counter() - started) * 1000.0, 3)
        state = payload["paths"][name]
        state["measured_frames"] += 1
        state["elapsed_ms"].append(record["elapsed_ms"])
        state["total_boxes"] += record["count"]
        if record["boxes"]:
            state["detected_frames"] += 1
        results[name] = record
    return results, False


def frame_line(record, total):
    index = record["frame_index"] + 1
    if not record.get("read_ok"):
        print("[帧 %02d/%d] 读帧失败 read=%sms 错误=%s"
              % (index, total, record.get("read_ms"), record.get("error")),
              flush=True)
        return
    parts = []
    for name, _, _ in PATHS:
        item = (record.get("paths") or {}).get(name) or {}
        if not item:
            parts.append(name + "=未测")
        elif item.get("boxes") is None:
            parts.append(name + "=" + ("未测" if "not_attempted" in
                                      (item.get("error") or "") else "err"))
        else:
            parts.append("%s=%d" % (name, len(item["boxes"])))
    production = record.get("production") or {}
    match = (record.get("comparison") or {}).get("match")
    diff = record.get("diff_prev_mean_abs")
    print("[帧 %02d/%d] read=%sms 处理=%sms 均值=%.1f 标准差=%.1f 暗=%.4f 亮=%.4f "
          "lap=%.1f sha=%s 前帧差=%s | %s 生产=%s 一致=%s"
          % (index, total, record.get("read_ms"), record.get("process_ms"),
             record.get("gray_mean") or 0.0, record.get("gray_std") or 0.0,
             record.get("dark_ratio_lt16") or 0.0,
             record.get("bright_ratio_gt240") or 0.0,
             record.get("laplacian_var") or 0.0,
             (record.get("gray_sha256") or "?")[:12],
             "n/a" if diff is None else "%.2f" % diff,
             " ".join(parts), production.get("count", "未测"),
             "未测" if match is None else ("是" if match else "否")),
          flush=True)


def run_frames(cv2, np, detector, cam, cascade, production, args, payload):
    prev = None
    for index in range(args.frames):
        started = time.perf_counter()
        record = {"frame_index": index, "read_ok": False}

        read_started = time.perf_counter()
        try:
            ok, frame, frame_ts = cam.read()
            read_exc = None
        except Exception as exc:
            ok, frame, frame_ts, read_exc = False, None, None, exc
        record["read_ms"] = round((time.perf_counter() - read_started) * 1000.0, 3)
        if read_exc is not None or not ok or frame is None:
            payload["camera"]["failed_frames"] += 1
            record["error"] = (exception_text(read_exc) if read_exc is not None
                               else "read_not_ok")
            err(payload, "frame_read", read_exc, frame=index,
                message="read() 失败（约 %sms）：%s"
                        % (record["read_ms"], record["error"]))
            payload["frames"].append(record)
            payload["aborted_at_frame"] = index
            payload["aborted_stage"] = "read"
            frame_line(record, args.frames)
            return

        payload["camera"]["valid_frames"] += 1
        record["read_ok"] = True
        height, width = int(frame.shape[0]), int(frame.shape[1])
        if payload["camera"]["actual_resolution"] is None:
            payload["camera"]["actual_resolution"] = "%dx%d" % (width, height)
            if (width, height) != (W, H):
                warn(payload, "实际分辨率 %s 与请求 %dx%d 不同"
                     % (payload["camera"]["actual_resolution"], W, H))

        process_started = time.perf_counter()
        try:
            gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
            record.update(stats(cv2, np, gray, prev, frame_ts,
                                frame.shape, frame.dtype))
            record["paths"], path_failed = run_paths(cv2, cascade, gray,
                                                      payload, index)
            if path_failed:
                record["process_ms"] = round(
                    (time.perf_counter() - process_started) * 1000.0, 3)
                payload["frames"].append(record)
                payload["aborted_at_frame"] = index
                payload["aborted_stage"] = "path"
                frame_line(record, args.frames)
                return
            prod_started = time.perf_counter()
            found = production.detect(frame) or []
            prod_ms = (time.perf_counter() - prod_started) * 1000.0
            production_boxes = sorted([list(item.bbox) for item in found])
            record["production"] = {"boxes": production_boxes,
                                    "count": len(production_boxes),
                                    "elapsed_ms": round(prod_ms, 3)}
            reference_path = payload["comparison"]["reference_path"]
            if reference_path is None:
                record["comparison"] = {"reference_path": None,
                                        "reference_filtered": None,
                                        "match": None}
            else:
                expected = sorted([list(item.bbox)
                                   for item in detector.suppress_nested_detections(
                                       [detector.FaceDetection(b[0], b[1], b[2], b[3], 0.99)
                                        for b in record["paths"][reference_path]["boxes"]])])
                record["comparison"] = {"reference_path": reference_path,
                                        "production": production_boxes,
                                        "reference_filtered": expected,
                                        "match": expected == production_boxes}
        except Exception as exc:
            record["process_ms"] = round(
                (time.perf_counter() - process_started) * 1000.0, 3)
            record["error"] = exception_text(exc)
            err(payload, "frame_process", exc, frame=index,
                message="本帧处理/生产检测抛错：%s" % exception_text(exc))
            payload["frames"].append(record)
            payload["aborted_at_frame"] = index
            payload["aborted_stage"] = "process"
            frame_line(record, args.frames)
            return

        record["process_ms"] = round(
            (time.perf_counter() - process_started) * 1000.0, 3)
        record["elapsed_ms"] = round(record["read_ms"] + record["process_ms"], 3)
        prev = gray

        state = payload["production"]
        state["measured_frames"] += 1
        state["elapsed_ms"].append(record["production"]["elapsed_ms"])
        state["total_boxes"] += record["production"]["count"]
        if production_boxes:
            state["detected_frames"] += 1

        comparison = payload["comparison"]
        if record["comparison"]["match"] is not None:
            comparison["checked_frames"] += 1
            if record["comparison"]["match"]:
                comparison["matched_frames"] += 1
            else:
                comparison["mismatched_frames"] += 1
                comparison["mismatch_frame_indices"].append(index)
                warn(payload, "帧 %d 生产框与 %s 过滤不一致（生产=%d 期望=%d）"
                     % (index, record["comparison"]["reference_path"],
                        len(production_boxes),
                        len(record["comparison"]["reference_filtered"])))

        payload["frames"].append(record)
        frame_line(record, args.frames)

        if index < args.frames - 1:
            remaining = args.period - (time.perf_counter() - started)
            if remaining > 0:
                time.sleep(remaining)
            record["cycle_ms"] = round(
                (time.perf_counter() - started) * 1000.0, 3)


def finalize(payload, args, started):
    camera = payload["camera"]
    frames = payload["frames"]
    payload["timing"] = {
        "read_ms": pcts([f["read_ms"] for f in frames if f.get("read_ok")]),
        "process_ms": pcts([f["process_ms"] for f in frames if "process_ms" in f]),
        "cycle_ms": pcts([f["cycle_ms"] for f in frames if "cycle_ms" in f]),
        "wall_ms": round((time.perf_counter() - started) * 1000.0, 2),
    }

    summary = payload["summary"]
    summary["requested"] = payload["request"]
    summary["valid_frames"] = camera["valid_frames"]
    summary["failed_frames"] = camera["failed_frames"]
    summary["not_attempted"] = (args.frames - camera["valid_frames"]
                                - camera["failed_frames"])
    summary["actual_resolution"] = camera["actual_resolution"]
    summary["paths"] = {}
    for name, _, _ in PATHS:
        state = payload["paths"][name]
        timing = pcts(state["elapsed_ms"])
        summary["paths"][name] = {
            "measured_frames": state["measured_frames"],
            "detected_frames": state["detected_frames"],
            "total_boxes": state["total_boxes"],
            "failed_frames": state["failed_frames"],
            "p50_ms": timing["p50"],
            "p95_ms": timing["p95"],
        }
    production = payload["production"]
    timing = pcts(production["elapsed_ms"])
    summary["production"] = {
        "backend": production["backend"],
        "measured_frames": production["measured_frames"],
        "detected_frames": production["detected_frames"],
        "total_boxes": production["total_boxes"],
        "p50_ms": timing["p50"],
        "p95_ms": timing["p95"],
    }
    summary["comparison_reference_path"] = payload["comparison"]["reference_path"]
    hashes = [f["gray_sha256"] for f in frames if f.get("gray_sha256")]
    distinct = len(set(hashes))
    summary["distinct_gray_sha256"] = distinct

    reasons = []
    if camera["valid_frames"] != args.frames:
        reasons.append("有效帧 %d/%d" % (camera["valid_frames"], args.frames))
    if camera["failed_frames"]:
        reasons.append("读帧失败 %d 次" % camera["failed_frames"])
    if payload["errors"]:
        reasons.append("errors=%d" % len(payload["errors"]))
    for name, _, _ in PATHS:
        if payload["paths"][name]["measured_frames"] != camera["valid_frames"]:
            reasons.append("%s 覆盖不足" % name)
    if production["measured_frames"] != camera["valid_frames"]:
        reasons.append("生产检测覆盖不足")
    if payload["comparison"]["checked_frames"] != camera["valid_frames"]:
        reasons.append("同帧校验覆盖不足")
    if payload["comparison"]["mismatched_frames"]:
        reasons.append("生产框与 %s 过滤不一致 %d 帧"
                       % (payload["comparison"]["reference_path"],
                          payload["comparison"]["mismatched_frames"]))
    complete = not reasons
    if distinct < len(hashes):
        warn(payload, "有效帧重复灰度（不同 hash %d/%d），可能静止画面或取流卡住"
             % (distinct, len(hashes)))

    payload["complete"] = complete
    payload["success"] = complete
    payload["exit_code"] = EXIT_OK if complete else EXIT_FAIL
    if payload["status"] == "running":
        payload["status"] = "complete" if complete else "partial"
    summary["complete"] = complete
    summary["incomplete_reasons"] = reasons
    summary["status"] = payload["status"]
    summary["exit_code"] = payload["exit_code"]


def finish(payload):
    try:
        with open(payload["json_path"], "w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
    except Exception as exc:
        sys.stderr.write("JSON 写入失败：%s\n" % exception_text(exc))
        print("[结论] status=%s complete=%s exit=%s"
              % (payload["status"], payload["complete"], payload["exit_code"]))
        return EXIT_FAIL
    print("[结论] status=%s complete=%s exit=%s 有效帧=%d/%d 错误=%d 警告=%d"
          % (payload["status"], payload["complete"], payload["exit_code"],
             payload["summary"]["valid_frames"],
             payload["summary"]["requested"]["frames"],
             len(payload["errors"]), len(payload["warnings"])))
    print("[JSON] 已写入 %s" % payload["json_path"])
    return int(payload["exit_code"])


def prepare_cascade(cv2, cfg):
    candidates = [str(cfg["detection"].get("haar_cascade") or ""), DEFAULT_HAAR]
    data = getattr(cv2, "data", None)
    if data is not None:
        candidates.append(os.path.join(getattr(data, "haarcascades", "") or "",
                                       "haarcascade_frontalface_default.xml"))
    for path in candidates:
        if path and os.path.isfile(path):
            classifier = cv2.CascadeClassifier(path)
            if classifier is not None and not classifier.empty():
                return path, classifier
    raise ProbeError("找不到可用的 Haar XML（试过：%s）"
                     % "；".join(p for p in candidates if p))


def main(argv=None):
    started = time.perf_counter()
    args = parse_args(argv)
    try:
        json_path = check_json(args.json_path)
    except ProbeError as exc:
        sys.stderr.write("参数错误：%s\n" % exc)
        return EXIT_FAIL
    payload = new_payload(args, json_path)

    try:
        import cv2
        import numpy as np
        from vision import camera, config, detector
    except Exception as exc:
        err(payload, "import", exc)
        payload["status"] = "import_failed"
        finalize(payload, args, started)
        return finish(payload)

    cam = None
    production = None
    stage = "config_load"
    try:
        cfg = config.load(PROFILE)
        payload["camera"]["warmup"] = int(cfg["camera"]["warmup_frames"])
        stage = "camera_open"
        cam = camera.OpenCVCamera(
            device=DEV, width=W, height=H, backend=BACKEND, mirror=False,
            warmup_frames=int(cfg["camera"]["warmup_frames"]))
        cam.open()
        payload["camera"]["opened"] = True
        stage = "camera_props"
        read_props(cv2, cam, payload)
        stage = "detector_setup"
        cascade_path, cascade = prepare_cascade(cv2, cfg)
        production = detector.create_detector(cfg)
        payload["production"]["backend"] = getattr(production, "backend", None)
        reference_path = None
        if getattr(production, "backend", None) == "haar":
            reference_path = ("equalized80" if getattr(production, "equalize", True)
                              else "plain80")
        payload["comparison"]["reference_path"] = reference_path
        if reference_path is None:
            warn(payload, "生产检测器不是 Haar，四路对照与生产同帧校验无法对齐")
        if getattr(production, "cascade_path", None) != cascade_path:
            warn(payload, "四路对照 XML（%s）与生产检测器不是同一个，"
                          "差异可能来自模型/路径" % cascade_path)
        stage = "frames"
        run_frames(cv2, np, detector, cam, cascade, production, args, payload)
    except Exception as exc:
        err(payload, stage, exc)
        payload["status"] = stage + "_failed"
    finally:
        if cam is not None:
            try:
                cam.release()
                payload["camera"]["released"] = True
            except Exception as exc:
                err(payload, "camera_release", exc)
        finalize(payload, args, started)
    return finish(payload)


if __name__ == "__main__":
    sys.exit(main())
