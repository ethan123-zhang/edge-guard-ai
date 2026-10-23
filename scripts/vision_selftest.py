"""环境自检 + 单帧基准 —— **上板第一天先跑这个，用它回答 V1**。

V1（memory/05）：Jetson 上的 OpenCV 到底有没有 FaceDetectorYN / FaceRecognizerSF？
单帧多少 ms？ 以前靠人肉试，现在两条命令：

    python3 scripts/vision_selftest.py                 # 板上（会自动跳过没有的模型）
    python scripts\\vision_selftest.py --json docs\\data\\vision-baseline-pc.json

输出分四段：环境 / 模型 / 人脸库 / 实测耗时。**耗时是"单帧检测+识别"，
不等于端到端延迟**，别拿它对外说（见 memory/02 的警告）。
"""
import argparse
import hashlib
import json
import os
import platform
import statistics
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

import cv2

from vision import camera, config, detector, gallery, recognizer


def log(message):
    print("%s %s" % (time.strftime("%H:%M:%S"), message))


def sha256_of(path):
    digest = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def percentile(values, ratio):
    if not values:
        return 0.0
    ordered = sorted(values)
    idx = min(len(ordered) - 1, max(0, int(round(ratio * (len(ordered) - 1)))))
    return ordered[idx]


def env_section(cfg):
    info = {
        "python": sys.version.split()[0],
        "platform": platform.platform(),
        "machine": platform.machine(),
        "cv2": cv2.__version__,
        "has_dnn": hasattr(cv2, "dnn"),
        "has_yunet_api": hasattr(cv2, "FaceDetectorYN"),
        "has_sface_api": hasattr(cv2, "FaceRecognizerSF"),
    }
    log("[环境] Python %s / OpenCV %s / %s" % (info["python"], info["cv2"], info["machine"]))
    log("        dnn=%s  FaceDetectorYN=%s  FaceRecognizerSF=%s"
        % (info["has_dnn"], info["has_yunet_api"], info["has_sface_api"]))
    return info


def model_section(cfg):
    det_model = config.resolve_vision(cfg["detection"]["model"])
    rec_model = config.resolve_vision(cfg["recognition"]["model"])
    info = {}
    for key, path in (("yunet", det_model), ("sface", rec_model)):
        exists = os.path.exists(path)
        entry = {"path": path, "exists": exists, "size": os.path.getsize(path) if exists else 0}
        if exists:
            entry["sha256"] = sha256_of(path)[:16]
        info[key] = entry
        log("[模型] %s：%s%s" % (
            key, "已就位" if exists else "**缺失**",
            "（%.1f MB, sha256 %s…）" % (entry["size"] / 1048576.0, entry["sha256"])
            if exists else "（先把模型放进 vision/models/）"))
    return info


def gallery_section(cfg):
    path = config.resolve_vision(cfg["recognition"]["gallery_dir"])
    store = gallery.FaceGallery(path).load()
    log("[人脸库] %s → %d 人" % (path, len(store)))
    for face_id, name in sorted(store.names().items()):
        log("          %s = %s" % (face_id, name))
    return {"path": path, "count": len(store), "names": store.names()}


def bench_section(cfg, source_spec, frames):
    det = detector.create_detector(cfg, log=log)
    try:
        rec = recognizer.create_recognizer(cfg, log=log)
    except recognizer.RecognizerUnavailable as exc:
        log("[基准] 识别器不可用：%s" % exc)
        rec = recognizer.NullRecognizer()

    if source_spec is None:
        source_spec = cfg["camera"]["source"]
    spec = source_spec
    if isinstance(spec, str) and spec.isdigit():
        spec = int(spec)
    source = camera.create_source(dict(cfg, camera=dict(cfg["camera"], source=spec)))

    detect_ms, embed_ms, faces_per_frame = [], [], []
    source.open()
    log("[基准] 视频源：%s" % source.describe())
    try:
        collected = 0
        attempts = 0
        while collected < frames and attempts < frames * 10:
            attempts += 1
            ok, frame, _ts = source.read()
            if not ok or frame is None:
                time.sleep(0.01)
                continue
            t0 = time.perf_counter()
            dets = det.detect(frame)
            t1 = time.perf_counter()
            detect_ms.append((t1 - t0) * 1000.0)
            for face in dets:
                e0 = time.perf_counter()
                rec.embed(frame, face)
                embed_ms.append((time.perf_counter() - e0) * 1000.0)
            faces_per_frame.append(len(dets))
            collected += 1
    finally:
        source.release()

    result = {
        "frames": len(detect_ms),
        "detector": det.describe(),
        "recognizer": rec.describe(),
        "detect_ms_p50": round(percentile(detect_ms, 0.50), 2),
        "detect_ms_p95": round(percentile(detect_ms, 0.95), 2),
        "embed_ms_p50": round(percentile(embed_ms, 0.50), 2) if embed_ms else 0.0,
        "faces_avg": round(statistics.mean(faces_per_frame), 2) if faces_per_frame else 0.0,
        "frames_with_face": sum(1 for n in faces_per_frame if n > 0),
    }
    log("[基准] %d 帧：检测 p50=%.1f ms / p95=%.1f ms；识别 p50=%.1f ms；平均 %.2f 张脸/帧"
        % (result["frames"], result["detect_ms_p50"], result["detect_ms_p95"],
           result["embed_ms_p50"], result["faces_avg"]))
    if result["frames_with_face"] == 0:
        log("[基准] 警告：一帧都没检测到人脸 —— 镜头前有人吗？光照够吗？Haar 尤其挑正面。")
    return result


def verdict(env, models, bench):
    lines = []
    yunet_ok = env["has_yunet_api"] and models["yunet"]["exists"]
    sface_ok = env["has_sface_api"] and models["sface"]["exists"]
    lines.append("V1 结论：YuNet %s，SFace %s" % (
        "可用" if yunet_ok else "不可用", "可用" if sface_ok else "不可用"))
    if not env["has_yunet_api"]:
        lines.append("  → OpenCV 版本太低：必须自编 4.5.4+（scripts/jetson-build-opencv.sh）")
    if not models["yunet"]["exists"]:
        lines.append("  → 缺模型文件：vision/models/face_detection_yunet_2023mar.onnx")
    if not models["sface"]["exists"]:
        lines.append("  → 缺模型文件：vision/models/face_recognition_sface_2021dec.onnx")
    if bench and bench["frames"]:
        lines.append("  单帧检测 p50 = %.1f ms（%d 帧样本）" % (bench["detect_ms_p50"], bench["frames"]))
    return lines


def main(argv=None):
    ap = argparse.ArgumentParser(description="哨兵管家 · 视觉环境自检 / V1 基准")
    ap.add_argument("--profile", default="sim")
    ap.add_argument("--config-dir", default=None)
    ap.add_argument("--source", default=None, help="覆盖摄像头序号/视频/图片目录")
    ap.add_argument("--frames", type=int, default=30)
    ap.add_argument("--no-camera", action="store_true", help="只查环境与模型，不实测")
    ap.add_argument("--json", default=None, help="把结果写成 JSON（建议放 docs/data/）")
    args = ap.parse_args(argv)

    cfg = config.load(args.profile, args.config_dir)
    log("[自检] profile=%s" % cfg["profile"])
    env = env_section(cfg)
    models = model_section(cfg)
    store = gallery_section(cfg)

    bench = None
    if not args.no_camera:
        try:
            bench = bench_section(cfg, args.source, max(3, args.frames))
        except (camera.SourceError, detector.DetectorUnavailable) as exc:
            log("[基准] 跳过：%s" % exc)
    else:
        log("[基准] --no-camera，跳过")

    print("")
    for line in verdict(env, models, bench):
        log(line)

    if args.json:
        payload = {"ts": int(time.time() * 1000), "profile": cfg["profile"],
                   "env": env, "models": models, "gallery": store, "bench": bench}
        parent = os.path.dirname(os.path.abspath(args.json))
        if parent:
            os.makedirs(parent, exist_ok=True)
        with open(args.json, "w", encoding="utf-8") as fh:
            json.dump(payload, fh, ensure_ascii=False, indent=2)
        log("[自检] 已写 %s" % args.json)
    return 0


if __name__ == "__main__":
    sys.exit(main())
