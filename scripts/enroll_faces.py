"""注册已知人脸：把照片/摄像头采集变成 `vision/face_db/<face_id>.npy`。

三种用法：

1) 用摄像头现场注册（交互式，要显示器）：
       python scripts\\enroll_faces.py --id p_10023 --name 王阿姨 --camera 0
   站到镜头前 → 按 SPACE 采集（建议 3 次，换个角度）→ 按 ENTER 保存 → q 取消

2) 用摄像头现场注册（**无头 / SSH 纯终端**，板子没显示器就用这个）：
       python3 scripts/enroll_faces.py --id p_10023 --name 王阿姨 --auto --profile board
   3 秒倒计时 → 连续取 5 帧（每 0.6 s）→ 逐张检测 → 质量门筛掉坏的 → 保存最好的那些。
   见 docs/无头注册与终端自检.md。

3) 用现成照片注册（批量，照片命名随意）：
       python scripts\\enroll_faces.py --id p_10023 --name 王阿姨 --images photos\\wang

`--auto` 还可以用 `--source <视频>` 在没摄像头的机器上自测同一条取帧/质量门代码路径：
       python scripts\\enroll_faces.py --auto --source demo.mp4 --samples 3 --id probe --name Probe

**前提**：必须有 SFace 模型（`vision/models/face_recognition_sface_2021dec.onnx`）
和 YuNet 模型（SFace 需要 YuNet 的 5 点关键点做对齐，Haar 没有关键点）。

板端 Python 3.6：不用 f-string / 海象运算符 / dataclass / `from __future__`，一律 `%` 格式化。
"""
import argparse
import copy
import math
import os
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

import cv2
import numpy as np

from vision import align, camera, config, detector, gallery, onnx_pipeline, recognizer
from vision.imageio import imread

IMAGE_EXTS = (".jpg", ".jpeg", ".png", ".bmp", ".webp")


def log(message):
    print("%s %s" % (time.strftime("%H:%M:%S"), message))
    sys.stdout.flush()


def largest(items):
    return max(items, key=lambda d: d.w * d.h) if items else None


def quality_text(quality):
    return "--" if quality is None else "%.3f" % quality


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


def countdown(seconds):
    """终端倒计时（无窗口、无 sleeps 技巧）：`3 秒后开始，请正对镜头`。"""
    total = int(math.ceil(max(0.0, float(seconds))))
    if total <= 0:
        return
    log("[enroll] %d 秒后开始，请正对镜头" % total)
    for left in range(total - 1, 0, -1):
        time.sleep(1.0)
        log("[enroll] 倒计时 %d" % left)
    time.sleep(1.0)


def capture_source(cfg, args):
    """按 `camera.source` 造源：`--source` 可写摄像头序号 / 视频文件 / 图片目录。

    摄像头走 `vision.camera.OpenCVCamera`（板端 v4l2+FOURCC 的参数全在配置里），
    视频/目录走 `VideoFileSource` / `ImageDirSource` —— 与生产同一个 `create_source`。
    """
    capt_cfg = copy.deepcopy(cfg)
    if args.source is not None:
        spec = str(args.source)
        capt_cfg["camera"]["source"] = int(spec) if spec.isdigit() else spec
    return camera.create_source(capt_cfg)


def enroll_from_auto(source, det, rec, samples, interval, wait_seconds, min_face_px,
                     quality_max):
    """无头采集：倒计时 → 逐帧检测 → 质量门 → 采纳，返回特征列表。

    质量门（与现场一致，参数全部来自配置）：
    - 人脸高度 >= `detection.min_face_px`（太小 = 太远，特征不可靠）
    - `align.quality()` 不为 None 且 <= `association.quality_max_rms`（越小越可信）
    - SFace 能对齐（`rec.embed` 不返回 None）
    """
    vectors = []
    accepted = 0
    source.open()
    try:
        log("[enroll] 采集源：%s" % source.describe())
        log("[enroll] 质量门：人脸高度 >= %dpx，对齐质量 <= %.2f（association.quality_max_rms）"
            % (min_face_px, quality_max))
        countdown(wait_seconds)
        log("[enroll] 开始采集：%d 张，间隔 %.1f 秒" % (samples, interval))
        for index in range(1, samples + 1):
            if index > 1:
                time.sleep(max(0.0, interval))
            ok, frame, _ts = source.read()
            if not ok or frame is None:
                log("[enroll] 第 %d 张：读不到帧 → 丢弃" % index)
                continue
            faces = det.detect(frame)
            if not faces:
                log("[enroll] 第 %d 张：没检测到人脸 → 丢弃" % index)
                continue
            face = largest(faces)
            quality = align.quality(face.landmarks) if getattr(face, "landmarks", None) else None
            head = "第 %d 张：框 %dpx 分数 %.2f 质量 %s" % (
                index, face.h, face.score, quality_text(quality))
            if face.h < min_face_px:
                log("[enroll] %s → 丢弃（人脸高度 < %dpx，离镜头近一点）"
                    % (head, min_face_px))
                continue
            if quality is None:
                log("[enroll] %s → 丢弃（没有关键点，SFace 无法对齐）" % head)
                continue
            if quality > quality_max:
                log("[enroll] %s → 丢弃（质量 > 门限 %.2f：正对镜头、避开背光/侧脸）"
                    % (head, quality_max))
                continue
            vector = rec.embed(frame, face)
            if vector is None:
                log("[enroll] %s → 丢弃（SFace 对齐失败，换成正脸再试）" % head)
                continue
            vectors.append(vector)
            accepted += 1
            log("[enroll] %s → 采纳" % head)
    finally:
        source.release()
    log("[enroll] 采集结束：采纳 %d/%d 张" % (accepted, samples))
    return vectors


def main(argv=None):
    ap = argparse.ArgumentParser(description="哨兵管家 · 注册已知人脸")
    ap.add_argument("--id", required=True, help="face_id，例如 p_10023")
    ap.add_argument("--name", required=True, help="显示名，例如 王阿姨")
    ap.add_argument("--profile", default="sim")
    ap.add_argument("--config-dir", default=None)
    ap.add_argument("--camera", type=int, default=None, help="摄像头序号（与 --images 二选一）")
    ap.add_argument("--images", default=None, help="照片目录（与 --camera 二选一）")
    ap.add_argument("--auto", action="store_true",
                    help="无头采集：不开窗口、不 waitKey，纯终端（SSH/板端用这个）")
    ap.add_argument("--source", default=None,
                    help="覆盖 camera.source（仅 --auto）：摄像头序号 / 视频文件 / 图片目录")
    ap.add_argument("--samples", type=int, default=5, help="无头采集取多少张（默认 5）")
    ap.add_argument("--interval", type=float, default=0.6, help="无头采集每张间隔秒数（默认 0.6）")
    ap.add_argument("--countdown", type=float, default=None,
                    help="无头采集开始前的倒计时秒数（默认：摄像头 3，视频文件 0）")
    ap.add_argument("--min-face-px", type=int, default=None,
                    help="覆盖 detection.min_face_px（人脸高度下限，默认跟配置）")
    ap.add_argument("--gallery-dir", default=None,
                    help="覆盖 recognition.gallery_dir（自测时指向临时目录，别污染人脸库）")
    ap.add_argument("--note", default=None, help="备注（仅打印，不入库）")
    args = ap.parse_args(argv)

    if args.auto and args.images:
        log("[enroll] --auto 与 --images 互斥：无头采集用摄像头/视频，照片注册用 --images")
        return 2
    if args.source is not None and not args.auto:
        log("[enroll] --source 只能配合 --auto 用（交互式现场注册用 --camera 即可）")
        return 2
    if args.auto and args.camera is not None:
        log("[enroll] --auto 用配置里的摄像头（或 --source），不要再给 --camera")
        return 2
    if not args.auto and (args.camera is None) == (args.images is None):
        log("[enroll] 必须且只能指定 --camera 或 --images 之一（无头采集用 --auto）")
        return 2
    if args.samples < 1:
        log("[enroll] --samples 至少为 1")
        return 2
    if args.interval < 0:
        log("[enroll] --interval 不能为负")
        return 2

    cfg = config.load(args.profile, args.config_dir)
    try:
        det, rec, mode = onnx_pipeline.create_components(cfg, log=log)
    except (detector.DetectorUnavailable, recognizer.RecognizerUnavailable,
            onnx_pipeline.PipelineUnavailable) as exc:
        log("[enroll] 模型没到位：%s" % exc)
        log("[enroll] 提示：Windows 中文路径下 OpenCV 读不了 ONNX（E-48），"
            "把模型拷到纯 ASCII 目录并用 --config-dir 指过去；板端无此问题")
        return 2
    log("[enroll] 后端：runtime.backend=%s → %s（%s / %s）"
        % (cfg["runtime"]["backend"], mode, det.describe(), rec.describe()))
    if not getattr(rec, "available", False):
        log("[enroll] 当前识别器是 none（没有 SFace 模型），无法注册。先补模型：")
        log("         vision\\models\\face_recognition_sface_2021dec.onnx")
        return 2
    if getattr(det, "backend", "") not in ("yunet", "yunet-onnx"):
        log("[enroll] 当前检测器是 %s（没有关键点），SFace 无法对齐 —— 先补 YuNet 模型，"
            "或把 runtime.backend 设成 cpu/auto 走裸 ONNX 管线。" % det.backend)
        return 2

    if args.auto:
        try:
            source = capture_source(cfg, args)
        except camera.SourceError as exc:
            log("[enroll] 视频源打不开：%s" % exc)
            return 2
        min_face_px = (int(args.min_face_px) if args.min_face_px is not None
                       else int(cfg["detection"]["min_face_px"]))
        quality_max = float(cfg["association"]["quality_max_rms"])
        wait_seconds = args.countdown
        if wait_seconds is None:
            # 摄像头要给人 3 秒站位；视频文件没人可站，直接开始
            is_file = args.source is not None and os.path.isfile(str(args.source))
            wait_seconds = 0.0 if is_file else 3.0
        try:
            vectors = enroll_from_auto(
                source, det, rec, int(args.samples), float(args.interval),
                float(wait_seconds), min_face_px, quality_max)
        except camera.SourceError as exc:
            log("[enroll] 采集中断：%s" % exc)
            return 2
    elif args.images:
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
        if args.auto:
            log("[enroll] --auto 未采纳：先用 scripts/camera_watch.py --profile board 看到画面/人脸，"
                "再检查光线、距离（1~2 m 正脸）、质量门 association.quality_max_rms")
        return 1

    mean = np.mean(np.stack(vectors, axis=0), axis=0)
    gallery_spec = (args.gallery_dir if args.gallery_dir is not None
                    else cfg["recognition"]["gallery_dir"])
    store = gallery.FaceGallery(config.resolve_vision(gallery_spec), log=log)
    store.load()
    store.add(args.id, args.name, mean)
    log("[enroll] 已保存 %s（%s），共 %d 个样本；人脸库现有 %d 人"
        % (args.id, args.name, len(vectors), len(store)))
    log("[enroll] 文件：%s" % os.path.join(store.dir, args.id + ".npy"))
    return 0


if __name__ == "__main__":
    sys.exit(main())