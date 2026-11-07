"""板端/SSH 纯终端自检：每秒一行，看摄像头有没有画面、检测跑多快、脸多大。

**为什么需要**：板子没有接显示器，SSH 下没有 X —— `cv2.imshow` 当场崩。
现场排障时最常问的三件事（"摄像头通不通 / 管线多少 fps / 人脸有多大"）必须能
在纯终端里回答，而且不能刷屏（一次 SSH 会话里刷几百行，前面的信息就看不见了）。

用法（板端，先按 board.yaml 的摄像头跑）：

    python3 scripts/camera_watch.py --profile board --duration 5
    python3 scripts/camera_watch.py --profile board                # 一直跑，Ctrl-C 退出
    python3 scripts/camera_watch.py --profile board --no-recognize # 只测检测/取景
    python3 scripts/camera_watch.py --profile board --jsonl logs/watch.jsonl
    python3 scripts/camera_watch.py --source demo.mp4 --duration 5 # 没有摄像头时看视频

组件装配与 `scripts/vision_sim.py` 一致：`onnx_pipeline.create_components(cfg)` 按
`runtime.backend` 选 ONNX 管线（auto/cpu/trt）或旧路径（legacy），检测器/识别器/摄像头
全部从 `vision.config.load(profile)` 读，参数不在这里硬编码。

板端 Python 3.6：不用 f-string / 海象运算符 / dataclass / `from __future__`，一律 `%` 格式化。
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

from vision import align, camera, config, detector, gallery, onnx_pipeline

# 每秒刷新一行：这是给现场人看的节奏，不是渲染帧率的节奏
REFRESH_SEC = 1.0
# 连续读不到多少帧就认定视频源挂了（摄像头掉了 / 文件没解码器）
MAX_READ_FAILURES = 30


def log(message):
    print("%s %s" % (time.strftime("%H:%M:%S"), message))
    sys.stdout.flush()


def parse_args(argv=None):
    ap = argparse.ArgumentParser(description="哨兵管家 · 摄像头/视觉管线终端自检")
    ap.add_argument("--profile", default="board", help="配置档：board / sim（vision/config/*.yaml）")
    ap.add_argument("--config-dir", default=None, help="配置目录（默认 vision/config）")
    ap.add_argument("--source", default=None,
                    help="覆盖 camera.source：摄像头序号 / 视频文件 / 图片目录")
    ap.add_argument("--duration", type=float, default=0.0, help="跑多少秒后退出（0=一直跑）")
    ap.add_argument("--no-recognize", action="store_true",
                    help="跳过 SFace 识别，只测检测/取景（板上省一半推理时间）")
    ap.add_argument("--jsonl", default=None, help="把每帧检测结果逐行写成 JSONL")
    return ap.parse_args(argv)


def apply_overrides(cfg, args):
    """只改运行时该改的：视频源覆盖。阈值/尺寸一律跟配置走，不在这里发明。"""
    if args.source is not None:
        spec = str(args.source)
        cfg["camera"]["source"] = int(spec) if spec.isdigit() else spec
    return cfg


def clock(seconds):
    """秒 → mm:ss（一眼看出跑了多久）。"""
    total = int(max(0.0, seconds))
    return "%02d:%02d" % (total // 60, total % 60)


def face_quality(face):
    """对齐质量：`align.quality()` = 对齐残差 ÷ 眼距，越小越好。没有关键点返回 None。"""
    points = getattr(face, "landmarks", None)
    if not points:
        return None
    return align.quality(points)


class Window(object):
    """一行输出对应一个刷新窗口（默认 1 秒）的状态累积器。"""

    def __init__(self):
        self.reset()

    def reset(self):
        self.max_faces = 0
        self.biggest = None      # (FaceDetection, frame)：这一秒里最大的人脸
        self.max_face_px = 0

    def on_frame(self, faces, frame):
        if not faces:
            return
        if len(faces) > self.max_faces:
            self.max_faces = len(faces)
        biggest = max(faces, key=lambda item: int(item.w) * int(item.h))
        if self.biggest is None or biggest.h > self.biggest[0].h:
            self.biggest = (biggest, frame)
        if biggest.h > self.max_face_px:
            self.max_face_px = biggest.h


def describe_face(face, quality):
    """人脸片段：`最大框=312px 分数=0.91 质量=0.07`。"""
    quality_text = "--" if quality is None else "%.2f" % quality
    return "最大框=%dpx 分数=%.2f 质量=%s" % (face.h, face.score, quality_text)


def recognize_biggest(biggest, rec, store, threshold):
    """对窗口里最大的那张脸做一次识别（每秒一次，不跟每一帧抢 CPU）。

    返回一段标签文本：`未知` / `未知（识别器 none）` / `已知=<face_id> conf=0.85`。
    """
    if biggest is None:
        return ""
    if rec is None or not getattr(rec, "available", False):
        return "未知（识别器 none）"
    if store is None:
        return "未知（空库）"
    face, frame = biggest
    vector = rec.embed(frame, face)
    if vector is None:
        return "未知（对齐失败）"
    hit = store.match(vector, threshold)
    if hit is None:
        return "未知"
    return "已知=%s conf=%.2f" % (hit.face_id, hit.score)


def build_components(cfg, want_recognize):
    """按 `runtime.backend` 装配检测器/识别器，并加载人脸库（--no-recognize 时跳过）。"""
    det, rec, mode = onnx_pipeline.create_components(cfg, log=log)
    log("[watch] 后端：runtime.backend=%s → %s（检测 %s / 识别 %s）"
        % (cfg["runtime"]["backend"], mode, det.describe(), rec.describe()))
    if not want_recognize:
        return det, None, None
    if not getattr(rec, "available", False):
        log("[watch] 识别器不可用（没有 SFace 模型），这一轮只测检测/取景")
        return det, rec, None
    store = gallery.FaceGallery(
        config.resolve_vision(cfg["recognition"]["gallery_dir"]), log=log)
    store.load()
    log("[watch] 人脸库：%s（%d 人）" % (store.dir, len(store)))
    return det, rec, store


def write_jsonl(handle, record):
    if handle is None:
        return
    handle.write(json.dumps(record, ensure_ascii=False) + "\n")


def describe_json_face(face):
    quality = face_quality(face)
    return {
        "bbox": [int(face.x), int(face.y), int(face.w), int(face.h)],
        "score": round(float(face.score), 4),
        "quality": None if quality is None else round(float(quality), 4),
    }


def run(cfg, args, det, rec, store, source):
    started = time.time()
    window = Window()
    frames = 0
    face_frames = 0
    max_face_px = 0
    read_failures = 0
    last_print = started

    handle = None
    if args.jsonl:
        parent = os.path.dirname(os.path.abspath(args.jsonl))
        if parent and not os.path.isdir(parent):
            os.makedirs(parent)
        handle = open(args.jsonl, "w", encoding="utf-8")
        log("[watch] 检测结果逐行写入：%s" % args.jsonl)

    log("[watch] 开始采样：每 %g 秒一行，%s"
        % (REFRESH_SEC, "Ctrl-C 退出" if args.duration <= 0 else "跑 %g 秒后自动退出" % args.duration))
    try:
        while True:
            now = time.time()
            if args.duration > 0 and (now - started) >= args.duration:
                break
            ok, frame, ts = source.read()
            if not ok or frame is None:
                read_failures += 1
                if read_failures >= MAX_READ_FAILURES:
                    log("[watch] 连续 %d 帧读不到画面，放弃（检查摄像头/USB 线/文件解码）"
                        % read_failures)
                    break
                continue
            read_failures = 0
            frames += 1
            faces = det.detect(frame)
            if faces:
                face_frames += 1
            window.on_frame(faces, frame)
            if window.max_face_px > max_face_px:
                max_face_px = window.max_face_px
            if handle is not None:
                # 只有真的要落盘才逐帧算质量/SVD，否则这条自检工具本身会被 IO 拖慢
                write_jsonl(handle, {
                    "frame": frames,
                    "ts_ms": int(ts),
                    "elapsed_s": round(time.time() - started, 3),
                    "detections": [describe_json_face(face) for face in faces],
                })

            now = time.time()
            if (now - last_print) < REFRESH_SEC:
                continue
            fps = frames / (now - started) if now > started else 0.0
            if args.no_recognize:
                tail = "| 仅检测（--no-recognize）"
                known = ""
            else:
                tail = "| 已知库=%d 人" % (len(store) if store is not None else 0)
                known = recognize_biggest(
                    window.biggest, rec, store, cfg["recognition"]["match_threshold"])
            if window.biggest is None:
                pieces = ["[%s]" % clock(now - started), "fps=%.1f" % fps,
                          "帧=%d" % frames, "人脸=0（画面里没人）"]
            else:
                face, _frame = window.biggest
                pieces = ["[%s]" % clock(now - started), "fps=%.1f" % fps,
                          "帧=%d" % frames, "人脸=%d" % window.max_faces,
                          describe_face(face, face_quality(face))]
                if known:
                    pieces.append(known)
            if tail:
                pieces.append(tail)
            print(" ".join(pieces))
            sys.stdout.flush()
            window.reset()
            last_print = now
    except KeyboardInterrupt:
        log("[watch] 收到 Ctrl-C，收尾")
    except cv2.error as exc:
        log("[watch] 检测/取景出错：%s" % exc)
        return 1
    finally:
        source.release()
        if handle is not None:
            elapsed = time.time() - started
            write_jsonl(handle, {
                "kind": "summary",
                "frames": frames,
                "elapsed_s": round(elapsed, 3),
                "avg_fps": round(frames / elapsed, 2) if elapsed > 0 else 0.0,
                "face_frame_ratio": round(float(face_frames) / frames, 4) if frames else 0.0,
                "max_face_px": max_face_px,
            })
            handle.close()

    elapsed = time.time() - started
    fps = frames / elapsed if elapsed > 0 else 0.0
    ratio = (100.0 * face_frames / frames) if frames else 0.0
    log("[watch] 结束：帧=%d 时长=%.1fs 平均fps=%.1f 有人脸帧=%.1f%% 最大脸=%dpx"
        % (frames, elapsed, fps, ratio, max_face_px))
    if frames == 0:
        log("[watch] 一帧都没拿到：源没打开/没出图，先用 --no-recognize 和 --jsonl 复查")
        return 1
    return 0


def main(argv=None):
    args = parse_args(argv)
    try:
        cfg = config.load(args.profile, args.config_dir)
    except config.ConfigError as exc:
        log("[watch] 配置不对：%s" % exc)
        return 2
    cfg = apply_overrides(cfg, args)

    try:
        det, rec, store = build_components(cfg, not args.no_recognize)
    except (detector.DetectorUnavailable, onnx_pipeline.PipelineUnavailable) as exc:
        log("[watch] 模型/管线没到位：%s" % exc)
        log("[watch] 提示：Windows 中文路径下 OpenCV 读不了 ONNX（E-48），"
            "把模型拷到纯 ASCII 目录并用 --config-dir 指过去；板端无此问题")
        return 2

    source = camera.create_source(cfg)
    try:
        source.open()
    except camera.SourceError as exc:
        log("[watch] 视频源打不开：%s" % exc)
        return 2

    log("[watch] profile=%s 视频源=%s" % (cfg["profile"], source.describe()))
    try:
        return run(cfg, args, det, rec, store, source)
    finally:
        source.release()


if __name__ == "__main__":
    sys.exit(main())