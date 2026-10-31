#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""JETSON-PROBE-01：Jetson 摄像头采集 / Haar 检测只读诊断。

只做三件事：
1. 用 V4L2（Linux）打开摄像头，读取固定数量的帧并测量采集耗时；
2. 若找到 Haar XML，则逐有效帧做正面人脸检测并测量检测耗时；
3. 打印结果，可用 --json 保存结构化结果。

本探针不做身份识别、不加载人脸库、不保存/输出图像或人名，也不启动
业务服务或 WebSocket。无人脸不是错误；Haar 不可用也不是采集失败，但会
明确标记“未检测”，绝不会把未检测说成识别通过。
"""
from __future__ import print_function

import argparse
import json
import os
import platform
import sys
import time

TASK = "JETSON-PROBE-01"
CASCADE_FILENAME = "haarcascade_frontalface_default.xml"
SYSTEM_CASCADE_DIRS = (
    "/usr/share/opencv4/haarcascades",
    "/usr/share/opencv/haarcascades",
)

EXIT_OK = 0
EXIT_CAMERA_OPEN = 1
EXIT_NO_VALID_FRAMES = 2
EXIT_JSON_WRITE = 3
EXIT_OPENCV_IMPORT = 4
EXIT_INTERNAL = 5


def positive_int(value):
    try:
        number = int(value)
    except (TypeError, ValueError):
        raise argparse.ArgumentTypeError("必须是正整数：%r" % value)
    if number <= 0:
        raise argparse.ArgumentTypeError("必须大于 0：%r" % value)
    return number


def non_negative_int(value):
    try:
        number = int(value)
    except (TypeError, ValueError):
        raise argparse.ArgumentTypeError("必须是非负整数：%r" % value)
    if number < 0:
        raise argparse.ArgumentTypeError("不能小于 0：%r" % value)
    return number


def device_value(value):
    text = str(value).strip()
    if not text:
        raise argparse.ArgumentTypeError("设备不能为空")
    # 数字按序号处理；其余按设备路径处理（如 /dev/video0）。
    if text.isdigit():
        return int(text)
    return text


def parse_args(argv=None):
    parser = argparse.ArgumentParser(
        description="JETSON-PROBE-01：摄像头采集与 Haar 检测诊断（不识别身份）",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument("--device", type=device_value, default=0,
                        help="摄像头序号或设备路径，例如 0 或 /dev/video0")
    parser.add_argument("--width", type=positive_int, default=1280,
                        help="请求采集宽度")
    parser.add_argument("--height", type=positive_int, default=720,
                        help="请求采集高度")
    parser.add_argument("--frames", type=positive_int, default=30,
                        help="预热后固定读取的帧数（含失败尝试）")
    parser.add_argument("--warmup", type=non_negative_int, default=5,
                        help="开始统计前读取并丢弃的帧数")
    parser.add_argument("--json", dest="json_path", default=None,
                        help="结果 JSON 文件路径；不自动创建父目录")
    parser.add_argument("--cascade", default=None,
                        help="可选的 Haar 级联 XML 路径（优先于内置/系统路径）")
    return parser.parse_args(argv)


def utc_now():
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def percentile(values, ratio):
    if not values:
        return None
    ordered = sorted(values)
    index = int(round(ratio * (len(ordered) - 1)))
    index = max(0, min(len(ordered) - 1, index))
    return round(float(ordered[index]), 2)


def exception_text(exc):
    try:
        text = str(exc)
    except Exception:
        text = repr(exc)
    text = " ".join(text.split())
    if len(text) > 800:
        text = text[:797] + "..."
    return text


def add_error(payload, stage, exc=None, frame_index=None, message=None):
    if message is None:
        message = exception_text(exc) if exc is not None else "未知错误"
    payload["errors"].append({
        "stage": stage,
        "frame_index": frame_index,
        "type": exc.__class__.__name__ if exc is not None else "ProbeError",
        "message": message,
    })


def add_warning(payload, message):
    payload["warnings"].append(message)


def build_payload(args):
    requested = "%dx%d" % (args.width, args.height)
    return {
        "task": TASK,
        "timestamp_utc": utc_now(),
        "status": "running",
        "exit_code": EXIT_INTERNAL,
        "success": False,
        "success_scope": "capture_and_optional_detection_only_not_identity_recognition",
        "python": sys.version.split()[0],
        "opencv": None,
        "has_dnn": False,
        "has_yunet_api": False,
        "has_sface_api": False,
        "platform": platform.platform(),
        "machine": platform.machine(),
        "camera": {
            "device": args.device,
            "device_arg": str(args.device),
            "backend": None,
            "opened": False,
            "requested_width": args.width,
            "requested_height": args.height,
            "requested_resolution": requested,
            "actual_width": None,
            "actual_height": None,
            "actual_resolution": None,
            "warmup_requested": args.warmup,
            "warmup_valid": 0,
            "warmup_failed": 0,
            "frames_requested": args.frames,
            "valid_frames": 0,
            "failed_frames": 0,
        },
        "capture": {
            "p50_ms": None,
            "p95_ms": None,
            "samples": 0,
            "scope": "有效帧的 cap.read() 耗时；不含预热和失败读取",
        },
        "detection": {
            "backend": "unavailable",
            "available": False,
            "cascade_path": None,
            "cascade_source": None,
            "cascade_candidates": [],
            "p50_ms": None,
            "p95_ms": None,
            "samples": 0,
            "timing_scope": "有效帧的 cvtColor(BGR2GRAY) + Haar detectMultiScale 耗时",
            "attempted_frames": 0,
            "failed_frames": 0,
            "frames_with_face": 0,
            "total_faces": 0,
            "faces_per_frame": [],
            "faces_per_frame_scope": "每个有效帧一项；null 表示该帧检测抛错，不是 0 张脸",
        },
        "recognition": {
            "performed": False,
            "verified": False,
            "missing_apis": [],
            "note": "本探针不执行身份识别；采集或检测通过不代表识别通过。",
        },
        "errors": [],
        "warnings": [],
        "notes": [
            "本探针不保存、不输出图像或人名，不读取人脸库。",
            "无人脸不是错误，也不等于身份识别验证。",
            "Haar 仅检测正面人脸，对光照、姿态和遮挡敏感。",
            "无 GPU 假设；本探针只使用 CPU 上的 OpenCV。",
        ],
    }


def populate_environment(cv2, payload):
    payload["opencv"] = getattr(cv2, "__version__", None)
    payload["has_dnn"] = bool(hasattr(cv2, "dnn"))
    payload["has_yunet_api"] = bool(hasattr(cv2, "FaceDetectorYN"))
    payload["has_sface_api"] = bool(hasattr(cv2, "FaceRecognizerSF"))

    missing = []
    if not payload["has_yunet_api"]:
        missing.append("cv2.FaceDetectorYN")
    if not payload["has_sface_api"]:
        missing.append("cv2.FaceRecognizerSF")
    payload["recognition"]["missing_apis"] = missing


def select_backend(cv2, payload):
    if sys.platform.startswith("linux"):
        v4l2 = getattr(cv2, "CAP_V4L2", None)
        if v4l2 is not None:
            return v4l2, "CAP_V4L2"
        add_warning(payload, "当前 OpenCV 没有 cv2.CAP_V4L2，回退到 CAP_ANY")
    any_backend = getattr(cv2, "CAP_ANY", 0)
    return any_backend, "CAP_ANY"


def cascade_candidates(cv2, explicit_path, payload):
    candidates = []
    if explicit_path:
        candidates.append((explicit_path, "--cascade"))

    try:
        cv2_data = getattr(cv2, "data", None)
        cv2_haar = getattr(cv2_data, "haarcascades", None) if cv2_data is not None else None
        if cv2_haar:
            candidates.append((os.path.join(cv2_haar, CASCADE_FILENAME), "cv2.data"))
        else:
            add_warning(payload, "cv2.data.haarcascades 不可用，继续尝试系统 Haar 路径")
    except Exception as exc:
        add_error(payload, "cascade_cv2_data", exc)

    for directory in SYSTEM_CASCADE_DIRS:
        candidates.append((os.path.join(directory, CASCADE_FILENAME), "system"))
    return candidates


def load_cascade(cv2, explicit_path, payload):
    detection = payload["detection"]
    candidates = cascade_candidates(cv2, explicit_path, payload)

    if not hasattr(cv2, "CascadeClassifier"):
        add_warning(payload, "当前 OpenCV 没有 CascadeClassifier，跳过检测但继续采集")
        detection["backend"] = "unavailable"
        detection["available"] = False
        return None

    seen = set()
    for path, source in candidates:
        normalized = os.path.normcase(os.path.abspath(path))
        if normalized in seen:
            continue
        seen.add(normalized)

        entry = {"path": path, "source": source, "loaded": False, "reason": None}
        detection["cascade_candidates"].append(entry)

        if not os.path.isfile(path):
            entry["reason"] = "not_found"
            if source == "--cascade":
                add_warning(payload, "--cascade 指定的 XML 不存在：%s" % path)
            continue

        try:
            classifier = cv2.CascadeClassifier(path)
            if classifier is None:
                entry["reason"] = "classifier_none"
                if source == "--cascade":
                    add_warning(payload, "--cascade 创建 CascadeClassifier 失败：%s" % path)
                continue
            if classifier.empty():
                entry["reason"] = "empty"
                if source == "--cascade":
                    add_warning(payload, "--cascade XML 无法加载或为空：%s" % path)
                continue
        except Exception as exc:
            entry["reason"] = "load_error"
            add_error(payload, "cascade_load", exc, message=exception_text(exc))
            if source == "--cascade":
                add_warning(payload, "--cascade 加载抛错，继续尝试后续候选：%s" % path)
            continue

        entry["loaded"] = True
        detection["backend"] = "haar"
        detection["available"] = True
        detection["cascade_path"] = path
        detection["cascade_source"] = source
        return classifier

    detection["backend"] = "unavailable"
    detection["available"] = False
    add_warning(
        payload,
        "Haar 级联不可用：仍会采集，但没有进行人脸检测（未检测不等于 0 张脸，也不影响采集验收）",
    )
    return None


def query_camera_size(cv2, cap, payload):
    camera = payload["camera"]
    for key, prop_name in (("actual_width", "CAP_PROP_FRAME_WIDTH"),
                           ("actual_height", "CAP_PROP_FRAME_HEIGHT")):
        prop = getattr(cv2, prop_name, None)
        if prop is None:
            continue
        try:
            value = cap.get(prop)
            value = int(round(float(value)))
        except Exception as exc:
            add_error(payload, "camera_get_" + key, exc)
            continue
        if value > 0:
            camera[key] = value


def update_size_from_frame(payload, frame):
    try:
        height, width = frame.shape[:2]
        width = int(width)
        height = int(height)
    except Exception:
        return
    if width > 0 and height > 0:
        payload["camera"]["actual_width"] = width
        payload["camera"]["actual_height"] = height


def set_camera_property(cv2, cap, payload, prop_name, value, label):
    prop = getattr(cv2, prop_name, None)
    if prop is None:
        add_warning(payload, "当前 OpenCV 没有 %s，无法请求%s" % (prop_name, label))
        return
    try:
        accepted = bool(cap.set(prop, value))
    except Exception as exc:
        add_error(payload, "camera_set_" + label, exc)
        return
    if not accepted:
        add_warning(payload, "摄像头拒绝请求%s=%s；以实际值为准" % (label, value))


def probe_camera(cv2, args, classifier, payload):
    camera = payload["camera"]
    detection = payload["detection"]
    capture_times = []
    detect_times = []
    cap = None

    backend_id, backend_name = select_backend(cv2, payload)
    camera["backend"] = backend_name

    try:
        try:
            cap = cv2.VideoCapture(args.device, backend_id)
        except TypeError as exc:
            # 极老接口不支持 backend 参数时仍尝试无 backend 构造。
            add_warning(payload, "VideoCapture(device, backend) 不受支持，回退无 backend 构造")
            try:
                cap = cv2.VideoCapture(args.device)
            except Exception as inner:
                add_error(payload, "camera_open", inner)
                cap = None
        except Exception as exc:
            add_error(payload, "camera_open", exc)
            cap = None

        opened = False
        if cap is not None:
            try:
                opened = bool(cap.isOpened())
            except Exception as exc:
                add_error(payload, "camera_is_opened", exc)
                opened = False

        camera["opened"] = opened
        if not opened:
            add_error(
                payload,
                "camera_open",
                message="摄像头打不开：device=%r backend=%s" % (args.device, backend_name),
            )
            return capture_times, detect_times

        set_camera_property(cv2, cap, payload, "CAP_PROP_FRAME_WIDTH", args.width, "width")
        set_camera_property(cv2, cap, payload, "CAP_PROP_FRAME_HEIGHT", args.height, "height")
        query_camera_size(cv2, cap, payload)

        for index in range(args.warmup):
            try:
                ok, frame = cap.read()
            except Exception as exc:
                camera["warmup_failed"] += 1
                add_error(payload, "warmup_read", exc, frame_index=index)
                continue
            if not ok or frame is None:
                camera["warmup_failed"] += 1
                add_error(
                    payload,
                    "warmup_read",
                    frame_index=index,
                    message="预热 read() 失败：ok=%r frame_is_none=%r" % (bool(ok), frame is None),
                )
                continue
            camera["warmup_valid"] += 1
            update_size_from_frame(payload, frame)

        for index in range(args.frames):
            started = time.perf_counter()
            try:
                ok, frame = cap.read()
            except Exception as exc:
                elapsed_ms = (time.perf_counter() - started) * 1000.0
                camera["failed_frames"] += 1
                add_error(
                    payload,
                    "frame_read",
                    exc,
                    frame_index=index,
                    message="read() 抛错（本次读取约 %.2f ms）：%s" % (elapsed_ms, exception_text(exc)),
                )
                continue
            elapsed_ms = (time.perf_counter() - started) * 1000.0

            if not ok or frame is None:
                camera["failed_frames"] += 1
                add_error(
                    payload,
                    "frame_read",
                    frame_index=index,
                    message=(
                        "read() 失败（本次读取约 %.2f ms）：ok=%r frame_is_none=%r"
                        % (elapsed_ms, bool(ok), frame is None)
                    ),
                )
                continue

            capture_times.append(elapsed_ms)
            camera["valid_frames"] += 1
            update_size_from_frame(payload, frame)

            if classifier is None:
                continue

            detection["attempted_frames"] += 1
            detect_started = time.perf_counter()
            try:
                gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
                faces = classifier.detectMultiScale(
                    gray,
                    scaleFactor=1.1,
                    minNeighbors=5,
                    minSize=(30, 30),
                )
                count = 0 if faces is None else len(faces)
                detect_ms = (time.perf_counter() - detect_started) * 1000.0
            except Exception as exc:
                detect_ms = (time.perf_counter() - detect_started) * 1000.0
                detection["failed_frames"] += 1
                detection["faces_per_frame"].append(None)
                add_error(
                    payload,
                    "face_detection",
                    exc,
                    frame_index=index,
                    message="Haar 检测抛错（约 %.2f ms）：%s" % (detect_ms, exception_text(exc)),
                )
                continue

            detect_times.append(detect_ms)
            detection["faces_per_frame"].append(int(count))
            detection["total_faces"] += int(count)
            if count > 0:
                detection["frames_with_face"] += 1

        return capture_times, detect_times
    finally:
        if cap is not None:
            try:
                cap.release()
            except Exception as exc:
                add_error(payload, "camera_release", exc)


def finalize_payload(payload, capture_times, detect_times):
    camera = payload["camera"]
    detection = payload["detection"]
    capture = payload["capture"]

    capture["samples"] = len(capture_times)
    capture["p50_ms"] = percentile(capture_times, 0.50)
    capture["p95_ms"] = percentile(capture_times, 0.95)

    detection["samples"] = len(detect_times)
    detection["p50_ms"] = percentile(detect_times, 0.50)
    detection["p95_ms"] = percentile(detect_times, 0.95)

    if camera["actual_width"] is not None and camera["actual_height"] is not None:
        camera["actual_resolution"] = "%dx%d" % (
            camera["actual_width"], camera["actual_height"]
        )

    if camera["opened"]:
        actual = camera["actual_resolution"]
        requested = camera["requested_resolution"]
        if actual is not None and actual != requested:
            add_warning(
                payload,
                "实际分辨率 %s 与请求分辨率 %s 不同（驱动/摄像头可能不支持请求值）"
                % (actual, requested),
            )

    if payload["status"] != "running":
        return

    if not camera["opened"]:
        payload["status"] = "camera_open_failed"
        payload["exit_code"] = EXIT_CAMERA_OPEN
        payload["success"] = False
        return

    if camera["valid_frames"] == 0:
        payload["status"] = "no_valid_frames"
        payload["exit_code"] = EXIT_NO_VALID_FRAMES
        payload["success"] = False
        return

    payload["success"] = True
    payload["exit_code"] = EXIT_OK

    if not detection["available"]:
        payload["status"] = "capture_ok_detection_unavailable"
    elif detection["failed_frames"] > 0 and detection["samples"] == 0:
        payload["status"] = "capture_ok_detection_failed"
    elif detection["failed_frames"] > 0:
        payload["status"] = "capture_ok_detection_partial"
    elif camera["failed_frames"] > 0:
        payload["status"] = "capture_ok_with_read_errors"
    elif detection["frames_with_face"] == 0:
        payload["status"] = "ok_no_faces"
    else:
        payload["status"] = "ok"

    if (detection["available"] and detection["samples"] > 0
            and detection["failed_frames"] == 0
            and detection["frames_with_face"] == 0):
        add_warning(
            payload,
            "所有有效帧均未检出人脸；这不是错误，也不等于身份识别验证，Haar 对正脸/光照要求较高",
        )
    if camera["failed_frames"] > 0:
        add_warning(payload, "采集期间有 %d 次读帧失败，详见 errors" % camera["failed_frames"])


def format_ms(value):
    return "n/a" if value is None else "%.2f" % value


def format_faces(faces):
    return "[" + ", ".join("null" if item is None else str(item) for item in faces) + "]"


def print_report(payload):
    camera = payload["camera"]
    detection = payload["detection"]
    recognition = payload["recognition"]

    print("")
    print("== %s ==" % TASK)
    print("[环境] Python %s | OpenCV %s | dnn=%s YuNet=%s SFace=%s"
          % (payload["python"], payload["opencv"] if payload["opencv"] is not None else "未加载",
             payload["has_dnn"], payload["has_yunet_api"], payload["has_sface_api"]))
    print("[摄像头] device=%r backend=%s opened=%s"
          % (camera["device"], camera["backend"], camera["opened"]))
    print("  请求分辨率=%s 实际分辨率=%s"
          % (camera["requested_resolution"], camera["actual_resolution"] or "未知"))
    if camera["opened"]:
        print("  预热：成功 %d / 失败 %d（共 %d 次）"
              % (camera["warmup_valid"], camera["warmup_failed"], camera["warmup_requested"]))
        print("  采集：有效 %d / 失败 %d（共 %d 次）"
              % (camera["valid_frames"], camera["failed_frames"], camera["frames_requested"]))
    print("[采集耗时] p50=%s ms p95=%s ms（有效帧样本 %d）"
          % (format_ms(payload["capture"]["p50_ms"]),
             format_ms(payload["capture"]["p95_ms"]),
             payload["capture"]["samples"]))

    if detection["available"]:
        print("[检测] backend=%s | cascade=%s (%s)"
              % (detection["backend"], detection["cascade_path"], detection["cascade_source"]))
        print("  检测耗时 p50=%s ms p95=%s ms（成功样本 %d）"
              % (format_ms(detection["p50_ms"]), format_ms(detection["p95_ms"]),
                 detection["samples"]))
        print("  检测帧=%d 检测失败=%d 有人脸帧=%d 总脸数=%d"
              % (detection["attempted_frames"], detection["failed_frames"],
                 detection["frames_with_face"], detection["total_faces"]))
        print("  每帧脸数=%s" % format_faces(detection["faces_per_frame"]))
    else:
        print("[检测] backend=unavailable：Haar 不可用，未进行人脸检测（不是 0 张脸）")
        print("  采集仍有效；未检测不代表识别通过，也不影响采集验收")

    print("[识别] performed=%s verified=%s missing_apis=%s"
          % (recognition["performed"], recognition["verified"],
             recognition["missing_apis"] or "无"))
    print("[范围] success 仅表示采集验收，不含身份识别")
    print("[结论] status=%s exit_code=%s success=%s"
          % (payload["status"], payload["exit_code"], payload["success"]))

    if payload["warnings"]:
        print("[警告] %d 条" % len(payload["warnings"]))
        for message in payload["warnings"][:5]:
            print("  - %s" % message)
    if payload["errors"]:
        print("[错误] %d 条" % len(payload["errors"]))
        for item in payload["errors"][:5]:
            print("  - %s frame=%s: %s"
                  % (item["stage"], item["frame_index"], item["message"]))


def write_json(path, payload):
    text = json.dumps(payload, ensure_ascii=False, indent=2)
    with open(path, "w", encoding="utf-8") as handle:
        handle.write(text)
        handle.write("\n")


def finish(payload, args):
    if args.json_path:
        try:
            write_json(args.json_path, payload)
        except Exception as exc:
            payload["exit_code"] = EXIT_JSON_WRITE
            print("JSON 写入失败：%s" % exception_text(exc), file=sys.stderr)
            print_report(payload)
            return EXIT_JSON_WRITE
        print_report(payload)
        print("[JSON] 已写入 %s" % args.json_path)
        return payload["exit_code"]

    print_report(payload)
    print("[JSON] 未指定 --json，仅打印")
    return payload["exit_code"]


def main(argv=None):
    args = parse_args(argv)
    payload = build_payload(args)

    try:
        import cv2
    except Exception as exc:
        add_error(payload, "opencv_import", exc)
        payload["status"] = "opencv_import_failed"
        payload["exit_code"] = EXIT_OPENCV_IMPORT
        payload["success"] = False
        return finish(payload, args)

    try:
        populate_environment(cv2, payload)
    except Exception as exc:
        add_error(payload, "environment_probe", exc)
        payload["status"] = "internal_error"
        payload["exit_code"] = EXIT_INTERNAL
        payload["success"] = False
        return finish(payload, args)

    # Haar 的意外异常也不能阻断采集；明确标记未检测后继续。
    try:
        classifier = load_cascade(cv2, args.cascade, payload)
    except Exception as exc:
        add_error(payload, "cascade_probe", exc)
        payload["detection"]["available"] = False
        payload["detection"]["backend"] = "unavailable"
        add_warning(payload, "Haar 探测异常；跳过检测但继续采集（未检测不等于 0 张脸）")
        classifier = None

    try:
        capture_times, detect_times = probe_camera(cv2, args, classifier, payload)
    except Exception as exc:
        add_error(payload, "probe", exc)
        payload["status"] = "internal_error"
        payload["exit_code"] = EXIT_INTERNAL
        payload["success"] = False
        capture_times, detect_times = [], []

    finalize_payload(payload, capture_times, detect_times)
    return finish(payload, args)


if __name__ == "__main__":
    sys.exit(main())
