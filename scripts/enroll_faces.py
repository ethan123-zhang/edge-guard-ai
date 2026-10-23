"""注册已知人脸：把照片变成 `vision/face_db/<face_id>.npy`。

两种用法：

1) 用摄像头现场注册（推荐，光照和现场一致）：
       python scripts\\enroll_faces.py --id p_10023 --name 王阿姨 --camera 0
   站到镜头前 → 按 SPACE 采集（建议 3 次，换个角度）→ 按 ENTER 保存 → q 取消

2) 用现成照片注册（批量，照片命名随意）：
       python scripts\\enroll_faces.py --id p_10023 --name 王阿姨 --images photos\\wang

**前提**：必须有 SFace 模型（`vision/models/face_recognition_sface_2021dec.onnx`）
和 YuNet 模型（SFace 需要 YuNet 的 5 点关键点做对齐，Haar 没有关键点）。
"""
import argparse
import os
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

import cv2
import numpy as np

from vision import camera, config, detector, gallery, recognizer
from vision.imageio import imread

IMAGE_EXTS = (".jpg", ".jpeg", ".png", ".bmp", ".webp")


def log(message):
    print("%s %s" % (time.strftime("%H:%M:%S"), message))


def largest(items):
    return max(items, key=lambda d: d.w * d.h) if items else None


def embedding_for(image, det, rec):
    """一张图 → 最大人脸的归一化特征。没有脸/没对齐返回 None。"""
    face = largest(det.detect(image))
    if face is None:
        return None
    return rec.embed(image, face)


def enroll_from_images(paths, det, rec):
    vectors = []
    for path in paths:
        image = imread(path)
        if image is None:
            log("[enroll] 读不了：%s" % path)
            continue
        vec = embedding_for(image, det, rec)
        if vec is None:
            log("[enroll] 没检测到可用人脸：%s" % os.path.basename(path))
            continue
        vectors.append(vec)
        log("[enroll] 采纳：%s" % os.path.basename(path))
    return vectors


def enroll_from_camera(device, backend, det, rec):
    source = camera.OpenCVCamera(device=device, width=1280, height=720, backend=backend,
                                 mirror=True, warmup_frames=3)
    source.open()
    vectors = []
    log("[enroll] 摄像头已开。SPACE=采集一帧  ENTER=保存  q=取消")
    window = "enroll (SPACE=capture, ENTER=save, q=quit)"
    try:
        while True:
            ok, frame, _ts = source.read()
            if not ok:
                continue
            faces = det.detect(frame)
            canvas = frame.copy()
            for face in faces:
                x, y, w, h = face.bbox
                cv2.rectangle(canvas, (x, y), (x + w, y + h), (0, 200, 0), 2)
            cv2.putText(canvas, "samples=%d" % len(vectors), (10, 24),
                        cv2.FONT_HERSHEY_SIMPLEX, 0.6, (255, 255, 255), 1, cv2.LINE_AA)
            cv2.imshow(window, canvas)
            key = cv2.waitKey(1) & 0xFF
            if key in (ord("q"), 27):
                log("[enroll] 已取消")
                return []
            if key == ord(" "):
                face = largest(faces)
                if face is None:
                    log("[enroll] 画面里没有人脸")
                    continue
                vec = rec.embed(frame, face)
                if vec is None:
                    log("[enroll] 特征提取失败（SFace 没对齐），挪近一点再试")
                    continue
                vectors.append(vec)
                log("[enroll] 已采集第 %d 帧" % len(vectors))
            if key in (10, 13):   # ENTER
                if vectors:
                    return vectors
                log("[enroll] 还没采集，先按 SPACE")
    finally:
        source.release()
        cv2.destroyAllWindows()


def main(argv=None):
    ap = argparse.ArgumentParser(description="哨兵管家 · 注册已知人脸")
    ap.add_argument("--id", required=True, help="face_id，例如 p_10023")
    ap.add_argument("--name", required=True, help="显示名，例如 王阿姨")
    ap.add_argument("--profile", default="sim")
    ap.add_argument("--config-dir", default=None)
    ap.add_argument("--camera", type=int, default=None, help="摄像头序号（与 --images 二选一）")
    ap.add_argument("--images", default=None, help="照片目录（与 --camera 二选一）")
    ap.add_argument("--note", default=None, help="备注（仅打印，不入库）")
    args = ap.parse_args(argv)

    if (args.camera is None) == (args.images is None):
        log("[enroll] 必须且只能指定 --camera 或 --images 之一")
        return 2

    cfg = config.load(args.profile, args.config_dir)
    try:
        det = detector.create_detector(cfg, log=log)
        rec = recognizer.create_recognizer(cfg, log=log)
    except (detector.DetectorUnavailable, recognizer.RecognizerUnavailable) as exc:
        log("[enroll] 模型没到位：%s" % exc)
        return 2
    if not getattr(rec, "available", False):
        log("[enroll] 当前识别器是 none（没有 SFace 模型），无法注册。先补模型：")
        log("         vision\\models\\face_recognition_sface_2021dec.onnx")
        return 2
    if getattr(det, "backend", "") != "yunet":
        log("[enroll] 当前检测器是 %s（没有关键点），SFace 无法对齐 —— 先补 YuNet 模型。"
            % det.backend)
        return 2

    if args.images:
        if not os.path.isdir(args.images):
            log("[enroll] 目录不存在：%s" % args.images)
            return 2
        paths = [os.path.join(args.images, n) for n in sorted(os.listdir(args.images))
                 if n.lower().endswith(IMAGE_EXTS)]
        vectors = enroll_from_images(paths, det, rec)
    else:
        vectors = enroll_from_camera(args.camera, cfg["camera"]["backend"], det, rec)

    if not vectors:
        log("[enroll] 没有采集到任何特征，未保存")
        return 1

    mean = np.mean(np.stack(vectors, axis=0), axis=0)
    store = gallery.FaceGallery(config.resolve_vision(cfg["recognition"]["gallery_dir"]), log=log)
    store.load()
    store.add(args.id, args.name, mean)
    log("[enroll] 已保存 %s（%s），共 %d 个样本；人脸库现有 %d 人"
        % (args.id, args.name, len(vectors), len(store)))
    log("[enroll] 文件：%s" % os.path.join(store.dir, args.id + ".npy"))
    return 0


if __name__ == "__main__":
    sys.exit(main())
