#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Jetson Nano 人脸检测 —— CPU 基线探针（一次性探针，非产品代码）

目的
----
在**不动当前环境**的前提下，量出这块 Jetson Nano 用现有 OpenCV（3.2.0，无 CUDA）
跑 ONNX 人脸检测的单帧耗时，作为"值不值得装 CUDA 版 OpenCV"的决策依据。

为什么是探针而不是 face_service.py
----------------------------------
V1 是个可行性问题（Jetson 能不能跑通检测+识别、多快），产出是**一个答案**。
所以本脚本刻意写得独立、无依赖、可丢弃 —— 不引用 vision/ 目录，不写库，不发 WebSocket。
拿到数字后它就完成使命了。正式实现按 docs/01 §4 与 docs/02 §3.1 另写。

设计要点
--------
1. **不依赖 YuNet**：本机 OpenCV 3.2.0 没有 cv2.FaceDetectorYN（需 ≥4.5.4）。
   改用 caffe 版 res10 SSD 人脸检测模型（OpenCV 官方示例模型，两个文件都很小）。
2. **预热后多次取平均**：第一次前向要做后端初始化，
   首帧必然极慢。不预热就报数会把数字严重高估。
3. **报告分位数而非只有均值**：Jetson 无散热会降频，均值会掩盖抖动。
4. **分辨率可切**：分辨率是最大的性能杠杆，一次跑多个分辨率看曲线。

用法
----
    python3 jetson_baseline_probe.py                  # 用默认测试图
    python3 jetson_baseline_probe.py --image a.jpg
    python3 jetson_baseline_probe.py --camera         # 用摄像头抓一帧
    python3 jetson_baseline_probe.py --reps 50

输出
----
控制台打印各分辨率下的 min/avg/p50/p95/max（毫秒），并把结果追加到
--out-csv（默认 baseline_probe.csv），方便直接并入 docs/data/latency.csv。
"""

from __future__ import print_function  # Python 3.6，保持兼容写法

import argparse
import os
import sys
import time

try:
    import cv2
except ImportError:
    print("FATAL: 无法 import cv2。这个探针必须在 Jetson 上、用带 cv2 的 python3 运行。")
    sys.exit(2)


# 要测试的分辨率。检测前会把图缩到这个尺度的宽度（保持长宽比）。
# 320 是"远距离小脸"的典型场景，1280 是 config.yaml 里的规划值。
DEFAULT_WIDTHS = [320, 640, 960, 1280]

# ── 为什么用 UltraFace 而不是 YuNet ──────────────────────────────────
# 正式方案（docs/01 §5 D2）是 YuNet + SFace，但 YuNet 需要 cv2.FaceDetectorYN，
# 那要 OpenCV ≥ 4.5.4；本机是 3.2.0，没有。所以在**装 CUDA 版 OpenCV 之前**，
# 探针要能跑起来，就必须用一个「通用 cv2.dnn 能加载」的模型。
#
# 选 UltraFace version-RFB-320（ONNX Model Zoo）的理由：
#   1. 只有 1.21 MB，下载极快（板子带宽差，见 memory/04 E-12）
#   2. **ONNX opset 9** —— 很老的 opset，OpenCV 3.2 有较大机会能解析
#   3. 专为边缘设备设计的轻量人脸检测器，正好对应我们的场景
#   4. 输出是纯卷积的 scores/boxes，不含 ONNX 自定义算子，通用性好
# 它是**探针专用**的替代品，不是最终选型；装好 CUDA 版 OpenCV 后仍回到 YuNet。
DEFAULT_ONNX = "version-RFB-320.onnx"

# UltraFace 的归一化常数（来自模型官方说明，不能照搬别的模型的值）
ULTRAFACE_MEAN = (127.0, 127.0, 127.0)
ULTRAFACE_SCALE = 1.0 / 128.0
ULTRAFACE_INPUT = (320, 240)  # (w, h)，注意是宽在前


def banner(msg):
    print("")
    print("=" * 68)
    print(msg)
    print("=" * 68)


def load_image(args):
    """返回 (image, source_desc)。图片缺失时报错退出，不静默造一张假图。"""
    if args.camera:
        cap = cv2.VideoCapture(0)
        if not cap.isOpened():
            print("FATAL: 打不开摄像头 /dev/video0。")
            print("  提示：JetPack 上 USB 摄像头有时要走 v4l2 后端，见 docs/04 §2.4。")
            sys.exit(3)
        # 丢掉前几帧：刚打开时曝光/白平衡还没稳定，第一帧常常是黑的
        for _ in range(10):
            cap.read()
        ok, frame = cap.read()
        cap.release()
        if not ok or frame is None:
            print("FATAL: 摄像头读帧失败。")
            sys.exit(3)
        return frame, "camera:/dev/video0"

    path = args.image
    if not os.path.isfile(path):
        print("FATAL: 找不到测试图: {0}".format(path))
        print("  请给一张有清晰正脸的照片，或用 --camera 现场拍一张。")
        sys.exit(3)
    img = cv2.imread(path)
    if img is None:
        print("FATAL: cv2 读不了这张图（格式不支持或文件损坏）: {0}".format(path))
        sys.exit(3)
    return img, path


def load_detector(onnx_path):
    # 先分清两种完全不同的失败：模块不存在 vs 模型解析不了。
    # 2026-10-16 实测踩过坑：Ubuntu 打包的 python3-opencv 3.2.0 **裁掉了 cv2.dnn**，
    # 报错是 "module 'cv2' has no attribute 'dnn'"。当时脚本把它误报成
    # "该版本解析不了这个 ONNX"，结论方向就错了 —— 两者处置完全不同：
    #   模块缺失 → 换/装 OpenCV，试任何模型都没用
    #   解析失败 → 可以换模型再试
    if not hasattr(cv2, "dnn"):
        print("FATAL: 本机 cv2 没有 dnn 模块（不是模型的问题）。")
        print("  cv2.__file__ = {0}".format(getattr(cv2, "__file__", "?")))
        print("  OpenCV 版本  = {0}".format(cv2.__version__))
        print("")
        print("  → 这**不是**模型格式问题，换任何模型都跑不了。")
        print("  → 常见原因：发行版打包时裁剪了 dnn（Ubuntu 的 python3-opencv 3.2.0 就是如此），")
        print("     或 dnn 在单独的 contrib 包里而 Python 绑定没装。")
        print("  → 结论：当前环境的 Python 视觉推理不可用，需先装带 DNN 的 OpenCV（建议 4.5.4+）。")
        sys.exit(4)

    if not os.path.isfile(onnx_path):
        print("FATAL: 找不到 ONNX 模型: {0}".format(onnx_path))
        print("  下载：wget https://github.com/onnx/models/raw/main/validated/vision/"
              "body_analysis/ultraface/models/version-RFB-320.onnx")
        sys.exit(4)
    try:
        net = cv2.dnn.readNetFromONNX(onnx_path)
    except Exception as exc:  # noqa: BLE001 - 探针要把真实原因打出来
        print("FATAL: readNetFromONNX 失败: {0}".format(exc))
        print("")
        print("本机 OpenCV {0}，dnn 模块存在但解析不了这个 ONNX。".format(cv2.__version__))
        print("→ 这**是**模型兼容性问题。可以先试别的模型（更老的 opset），")
        print("  但若 OpenCV 过旧，直接装 CUDA 版 OpenCV 4.5+ 更省时间。")
        sys.exit(4)
    return net


def resize_keep_ratio(img, target_w):
    """把图缩放到指定宽度，保持长宽比。返回缩放后的图与缩放因子。"""
    h, w = img.shape[:2]
    if target_w >= w:
        return img, 1.0
    scale = float(target_w) / float(w)
    return cv2.resize(img, (int(w * scale), int(h * scale))), scale


def detect_once(net, img, conf_threshold):
    """跑一次前向 + NMS，返回 (人脸数, 最高置信度)。

    注意：模型输入固定 320x240，与传入图的实际分辨率无关；
    我们测的是「把整帧缩到不同分辨率再送进固定输入」的端到端成本，
    这正是真实流水线里的样子（分辨率影响的是 resize 与后续处理成本）。
    """
    # UltraFace 要求输入是 RGB，且按官方 mean/scale 归一化
    rgb = cv2.cvtColor(img, cv2.COLOR_BGR2RGB)
    blob = cv2.dnn.blobFromImage(
        rgb,
        scalefactor=ULTRAFACE_SCALE,
        size=ULTRAFACE_INPUT,
        mean=ULTRAFACE_MEAN,
        swapRB=False,       # 上面已经手动转 RGB 了，这里不能再换
        crop=False,
    )
    net.setInput(blob)
    out = net.forward()

    # UltraFace 输出两个张量：scores (1, N, 2) 与 boxes (1, N, 4)
    # 有些版本 cv2 返回的是单个 4 维数组，这里两种形状都兼容处理
    if isinstance(out, (list, tuple)) and len(out) >= 2:
        scores, boxes = out[0], out[1]
    else:
        arr = out[0] if isinstance(out, (list, tuple)) else out
        # 尝试按最后一维切分
        scores = arr[..., :2]
        boxes = arr[..., 2:6] if arr.shape[-1] >= 6 else arr[..., 2:]

    scores = scores.reshape(-1, scores.shape[-1]) if scores.ndim > 2 else scores
    boxes = boxes.reshape(-1, boxes.shape[-1]) if boxes.ndim > 2 else boxes

    confs = scores[:, 1]  # 第 1 列是"是人脸"的分数
    keep = []
    conf_vals = []
    for i in range(len(confs)):
        if confs[i] >= conf_threshold:
            # 模型输出的是相对坐标(0~1)，转成像素
            h, w = img.shape[:2]
            x1 = max(0.0, min(1.0, float(boxes[i][0]))) * w
            y1 = max(0.0, min(1.0, float(boxes[i][1]))) * h
            x2 = max(0.0, min(1.0, float(boxes[i][2]))) * w
            y2 = max(0.0, min(1.0, float(boxes[i][3]))) * h
            if x2 > x1 and y2 > y1:
                keep.append([x1, y1, x2 - x1, y2 - y1])
                conf_vals.append(float(confs[i]))

    if not keep:
        return 0, 0.0

    # NMS 去掉重叠框
    idxs = cv2.dnn.NMSBoxes(keep, conf_vals, conf_threshold, 0.3)
    if idxs is None or len(idxs) == 0:
        return 0, 0.0
    # 不同 OpenCV 版本 NMSBoxes 返回形状不同（有的返回 [[i],[j]]）
    try:
        n = len(idxs)
    except TypeError:
        n = 1
    return n, max(conf_vals)


def percentile(sorted_vals, pct):
    """Python 3.6 没有 statistics.quantiles，自己算，避免额外依赖。"""
    if not sorted_vals:
        return 0.0
    k = (len(sorted_vals) - 1) * (pct / 100.0)
    lo = int(k)
    hi = min(lo + 1, len(sorted_vals) - 1)
    frac = k - lo
    return sorted_vals[lo] * (1 - frac) + sorted_vals[hi] * frac


def bench(net, img, width, reps, warmup, conf_threshold):
    small, scale = resize_keep_ratio(img, width)
    h, w = small.shape[:2]

    # 预热：不计入统计。第一次前向要走后端的层初始化/内存分配，
    # 首帧往往是稳态的数倍，不预热会把结果严重高估。
    for _ in range(warmup):
        detect_once(net, small, conf_threshold)

    times = []
    faces = 0
    top_conf = 0.0
    for _ in range(reps):
        t0 = time.time()
        faces, top_conf = detect_once(net, small, conf_threshold)
        t1 = time.time()
        times.append((t1 - t0) * 1000.0)

    times.sort()
    return {
        "width": w,
        "height": h,
        "faces": faces,
        "top_conf": top_conf,
        "min_ms": times[0],
        "avg_ms": sum(times) / len(times),
        "p50_ms": percentile(times, 50),
        "p95_ms": percentile(times, 95),
        "max_ms": times[-1],
    }


def main():
    ap = argparse.ArgumentParser(description="Jetson CPU 人脸检测基线探针（一次性）")
    ap.add_argument("--image", default="test.jpg", help="测试图路径（默认 test.jpg）")
    ap.add_argument("--camera", action="store_true", help="改用摄像头抓一帧作为测试图")
    ap.add_argument("--onnx", default=DEFAULT_ONNX, help="UltraFace ONNX 模型路径")
    ap.add_argument("--reps", type=int, default=30, help="每个分辨率重复次数")
    ap.add_argument("--warmup", type=int, default=3, help="预热次数（不计入统计）")
    ap.add_argument("--conf", type=float, default=0.5, help="置信度阈值")
    ap.add_argument("--widths", default=",".join(str(x) for x in DEFAULT_WIDTHS),
                    help="要测的宽度列表，逗号分隔")
    ap.add_argument("--out-csv", default="baseline_probe.csv", help="结果追加到这个 CSV")
    args = ap.parse_args()

    widths = [int(x) for x in args.widths.split(",") if x.strip()]

    banner("环境信息（这些数字要一起记录，否则耗时无法解读）")
    print("OpenCV      : {0}".format(cv2.__version__))
    print("Python      : {0}".format(sys.version.split()[0]))
    try:
        import multiprocessing
        print("CPU 核数    : {0}".format(multiprocessing.cpu_count()))
    except Exception:
        pass
    has_cuda = hasattr(cv2, "cuda")
    print("cv2.cuda    : {0}".format("有模块" if has_cuda else "无（本机 OpenCV 未编 CUDA）"))
    if has_cuda:
        try:
            print("CUDA 设备数 : {0}".format(cv2.cuda.getCudaEnabledDeviceCount()))
        except Exception as exc:  # noqa: BLE001
            print("CUDA 设备数 : 查询失败 {0}".format(exc))

    # 散热状态：Jetson 跑热会降频，不记录温度的数字不可信
    for zone in ("/sys/devices/virtual/thermal/thermal_zone0/temp",):
        if os.path.isfile(zone):
            try:
                with open(zone) as fh:
                    print("起始温度    : {0:.1f} C".format(int(fh.read().strip()) / 1000.0))
            except Exception:
                pass

    img, source = load_image(args)
    h0, w0 = img.shape[:2]
    print("测试图      : {0}  ({1}x{2})".format(source, w0, h0))

    net = load_detector(args.onnx)

    banner("基准测试（每个分辨率先预热 {0} 次，再测 {1} 次）".format(args.warmup, args.reps))
    print("{0:>7} {1:>10} {2:>8} {3:>8} {4:>8} {5:>8} {6:>8} {7:>9} {8:>7}".format(
        "宽度", "实际尺寸", "人脸数", "最高分", "min", "avg", "p50", "p95", "max"))
    print("-" * 78)

    rows = []
    for width in widths:
        r = bench(net, img, width, args.reps, args.warmup, args.conf)
        rows.append(r)
        print("{0:>7} {1:>10} {2:>8} {3:>8.2f} {4:>8.1f} {5:>8.1f} {6:>8.1f} {7:>9.1f} {8:>7.1f}".format(
            r["width"], "{0}x{1}".format(r["width"], r["height"]), r["faces"], r["top_conf"],
            r["min_ms"], r["avg_ms"], r["p50_ms"], r["p95_ms"], r["max_ms"]))

    banner("解读方向")
    best = min(rows, key=lambda x: x["avg_ms"])
    print("最快：{0}x{1} 平均 {2:.1f} ms/帧（约 {3:.1f} FPS）".format(
        best["width"], best["height"], best["avg_ms"], 1000.0 / best["avg_ms"]))
    print("")
    print("判据（对应 docs/01 §3.1 与 docs/06 降级预案）：")
    print("  avg < 200ms  → 值得装 CUDA 版 OpenCV，加速后余量充足")
    print("  200~500ms    → 装 CUDA 版有收益，但仍需降分辨率或跳帧")
    print("  avg > 500ms  → 即使 GPU 加速 3~5 倍也偏紧，应考虑改 V1 方案")
    print("")
    print("注意：这只是**检测**，不含识别（SFace 另算）。端到端还要加识别的耗时。")

    # 追加写 CSV，不覆盖历史（可以不同时间跑多次对比散热影响）
    write_header = not os.path.isfile(args.out_csv)
    try:
        with open(args.out_csv, "a") as fh:
            if write_header:
                fh.write("date,opencv,python,width,height,faces,top_conf,min_ms,avg_ms,p50_ms,p95_ms,max_ms,reps,image\n")
            import datetime
            today = datetime.date.today().isoformat()
            for r in rows:
                fh.write("{0},{1},{2},{3},{4},{5},{6:.2f},{7:.1f},{8:.1f},{9:.1f},{10:.1f},{11:.1f},{12},{13}\n".format(
                    today, cv2.__version__, sys.version.split()[0],
                    r["width"], r["height"], r["faces"], r["top_conf"],
                    r["min_ms"], r["avg_ms"], r["p50_ms"], r["p95_ms"], r["max_ms"],
                    args.reps, source))
        print("")
        print("结果已追加到 {0}".format(os.path.abspath(args.out_csv)))
    except Exception as exc:  # noqa: BLE001
        print("警告：写 CSV 失败（不影响结果）: {0}".format(exc))

    print("")
    print("请把以上**完整输出**贴回来，特别是：OpenCV 版本、各分辨率 avg/p95、")
    print("以及测试图里实际有几个人脸（用来确认检测真的在工作，而不是一直在报 0）。")


if __name__ == "__main__":
    main()
