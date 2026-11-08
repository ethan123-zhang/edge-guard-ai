"""门口素材录制（PC + USB 摄像头）：预览 → 按 T 开始录 → 按 C 停止。

**为什么单独写一个**：真实门口素材是关联层/质量门的验收前置条件，而
`docs/独立正负例素材采集与标注协议` 要求固定机位、每段只演一个场景、30–60 s。
现场操作最怕"以为在录、其实没录上"或"录完才发现编码坏了"，所以这个脚本：

1. 先给**实时预览**（镜位/光线不对当场能看出来），准备好再按 T；
2. 录制时窗口左上角红点 + 已录时长 + 文件名，按 C 立刻收口；
3. 收口后**自动回读**：重新打开文件读帧数、分辨率、实际帧率 —— 0 帧或解不开
   直接 exit 1，不让你带着空文件去标；
4. 缺省写到 `datasets/scenarios/`（已 gitignore），并顺手写一份
   `<视频>.capture.json`（sha256/帧数/实测帧率/摄像头参数），标注协议要用的
   元数据先备好，人工标注（`scenario-annotation@1`）仍然单独写。

按键（窗口要有焦点）：**T** 开始、**C** 停止、**Q**/**Esc** 退出（录制中按 Q 会先收口）。
窗口里的字用英文：cv2 画中文会变方块；中文提示都打在控制台。

用法：
    py -3.12 scripts/record_camera.py                       # 预览，T 开录，C 停
    py -3.12 scripts/record_camera.py --name sc01_replacement
    py -3.12 scripts/record_camera.py --list                 # 本机有哪些摄像头、各自多少帧
    py -3.12 scripts/record_camera.py --headless --seconds 5  # 无人值守自检（不弹窗）

**本机摄像头索引**（2026-11-08 实测，`--list` 可复核）：`0` = 笔记本内置
HP True Vision FHD（720p/1080p 都只有 ~26 fps），`1` = 外接 **HD USB Camera**
（就是之前接在 Jetson 上的那颗；**720p 只有 8.7 fps，1080p 才是 30 fps**）。
所以本脚本默认 `--camera 1 --width 1920 --height 1080`；换机器或换口先跑 `--list`。

后端：默认 MSMF（同一颗外接相机在 MSMF 下 1080p 能到 30 fps，DSHOW 只给 5–10 fps）。
Windows 的 OpenCV 不支持按设备名打开，只能用索引；`--list` 会尽量打印设备友好名
（装了 `pygrabber` 才有，纯可选）。

Python 3.6 语法（板端 test_py36_compat 会做 AST 检查）；本脚本只在有显示器的 PC 上用。
"""
import argparse
import hashlib
import os
import sys
import time

import cv2

DEFAULT_DIR = os.path.join("datasets", "scenarios")
DEFAULT_CODEC = "mp4v"
CODEC_EXT = {"mp4v": "mp4", "avc1": "mp4", "h264": "mp4", "XVID": "avi", "MJPG": "avi"}
HUD_FONT = cv2.FONT_HERSHEY_SIMPLEX
HUD_ORANGE = (0, 165, 255)
HUD_GREEN = (0, 220, 0)
HUD_RED = (0, 0, 255)
HUD_WHITE = (255, 255, 255)


# ---------------------------------------------------------------- 纯函数（可单测）

def unique_path(directory, stem, ext):
    """目录里已有同名文件就加 `_2`、`_3`…，绝不覆盖上一段素材。"""
    path = os.path.join(directory, "%s.%s" % (stem, ext))
    counter = 2
    while os.path.exists(path):
        path = os.path.join(directory, "%s_%d.%s" % (stem, counter, ext))
        counter += 1
    return path


def format_hms(seconds):
    """`75.4` → `01:15`；录制时显示用。"""
    total = int(max(0.0, float(seconds)))
    return "%02d:%02d" % (total // 60, total % 60)


def default_stem(now=None):
    return "rec_%s" % time.strftime("%Y%m%d_%H%M%S", time.localtime(now))


def sha256_file(path):
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def verify_recording(path):
    """回读刚写完的视频：返回 `(ok, 信息dict)`。

    这是本脚本最重要的一段：宁可当场报错，也不要让人拿着 0 帧文件去标 4 段素材。
    """
    info = {"path": path, "frames": 0, "fps": 0.0, "width": 0, "height": 0,
            "size_bytes": 0, "problem": None}
    if not os.path.exists(path):
        info["problem"] = "文件不存在"
        return False, info
    info["size_bytes"] = os.path.getsize(path)
    if info["size_bytes"] <= 0:
        info["problem"] = "文件是 0 字节"
        return False, info
    cap = cv2.VideoCapture(path)
    if not cap.isOpened():
        info["problem"] = "OpenCV 打不开这个文件（编码没生效？）"
        return False, info
    try:
        info["fps"] = float(cap.get(cv2.CAP_PROP_FPS) or 0.0)
        info["width"] = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH) or 0)
        info["height"] = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT) or 0)
        while True:
            ok, _frame = cap.read()
            if not ok:
                break
            info["frames"] += 1
    finally:
        cap.release()
    if info["frames"] <= 0:
        info["problem"] = "一帧都读不出来"
        return False, info
    return True, info


# ---------------------------------------------------------------- 摄像头 / 写盘

def estimate_fps(stamps, window=45):
    """用相邻帧时间戳估真实采集帧率（不是信任 `CAP_PROP_FPS` 的标称值）。

    为什么必须估：这台 UVC 在 720p/MJPG 下，DSHOW 协商到 **13.6 fps**、MSMF 才有
    **27.8 fps**；若按标称 30 fps 写盘，回放会整体加速一倍，标注的 `t_ms` 就全错。
    """
    tail = list(stamps)[-int(window):]
    if len(tail) < 3:
        return None
    span = tail[-1] - tail[0]
    if span <= 0:
        return None
    return (len(tail) - 1) / span


def measure_capture_fps(cap, count=30, discard=15, log=None):
    """开流后先丢若干帧（MSMF/DSHOW 头几帧很慢），再按时间戳测稳态帧率。

    返回 `(fps 或 None, 最后一帧)`。headless 与交互式开录前都用它定写盘帧率，
    避免"按 30 fps 写、实际 13.6 fps"导致回放整体加速。
    """
    for _ in range(int(discard)):
        cap.read()
    stamps = []
    frame = None
    while len(stamps) < int(count):
        ok, got = cap.read()
        if not ok or got is None:
            break
        stamps.append(time.perf_counter())
        frame = got
    if log is not None and len(stamps) < int(count):
        log("  [warn] 量帧率时只读到 %d/%d 帧" % (len(stamps), count))
    return estimate_fps(stamps, window=count), frame


def sane_write_fps(fps_estimate, requested):
    """定写盘帧率：明显慢就按实测写，接近请求值就按请求值写。

    这台 UVC 的两种慢法：DSHOW 只给 13.6 fps（明显低于 30 → 必须按 13.6 写，
    否则回放加速一倍）；MSMF 偶发 70 ms 停顿会让 1 秒内的估计偏低到 25，
    但整段稳态是 30（→ 按 30 写，别把视频拉慢）。收口时还会用实际帧数复核，
    偏离超过 15% 直接告警。
    """
    requested = float(requested or 30.0)
    if not fps_estimate or fps_estimate < 5.0:
        return requested
    if fps_estimate < requested * 0.75:
        return round(fps_estimate, 1)
    return requested


def backend_candidates(requested):
    """按优先级给出要试的 OpenCV 后端。

    Windows 上 **MSMF 优先**：实测同一摄像头 720p/MJPG，MSMF 27.8 fps、DSHOW 只有
    13.6 fps（DSHOW 把 MJPG 协商成了 15 fps 档）；MSMF 打不开再退 DSHOW。
    """
    if requested in ("dshow", "msmf"):
        return [(requested.upper(), cv2.CAP_DSHOW if requested == "dshow" else cv2.CAP_MSMF)]
    if requested == "any":
        return [("ANY", cv2.CAP_ANY)]
    if os.name == "nt":
        return [("MSMF", cv2.CAP_MSMF), ("DSHOW", cv2.CAP_DSHOW)]
    return [("ANY", cv2.CAP_ANY)]


def open_camera(args, log):
    """打开摄像头并协商格式；返回 `(cap, 实际参数dict)`。"""
    last_error = None
    for name, api in backend_candidates(args.backend):
        cap = cv2.VideoCapture(args.camera, api)
        if not cap.isOpened():
            cap.release()
            last_error = "%s 打不开 index=%d" % (name, args.camera)
            log("  [warn] %s，换下一个后端" % last_error)
            continue
        # 顺序照抄 vision/camera.py：FOURCC → FPS → 分辨率 → 缓冲
        cap.set(cv2.CAP_PROP_FOURCC, cv2.VideoWriter_fourcc(*args.fourcc))
        cap.set(cv2.CAP_PROP_FPS, float(args.fps))
        cap.set(cv2.CAP_PROP_FRAME_WIDTH, float(args.width))
        cap.set(cv2.CAP_PROP_FRAME_HEIGHT, float(args.height))
        cap.set(cv2.CAP_PROP_BUFFERSIZE, 1)
        for _ in range(5):                       # 丢掉开流时的黑/花帧
            cap.read()
        ok, frame = cap.read()
        if not ok or frame is None:
            cap.release()
            last_error = "%s 打开了但读不到帧" % name
            log("  [warn] %s" % last_error)
            continue
        actual = {
            "backend": name,
            "width": int(cap.get(cv2.CAP_PROP_FRAME_WIDTH)),
            "height": int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT)),
            "fps": float(cap.get(cv2.CAP_PROP_FPS) or 0.0),
            "frame_shape": tuple(frame.shape),
        }
        fps_est, _last = measure_capture_fps(cap, count=24, discard=6, log=log)
        actual["measured_fps"] = fps_est
        if fps_est and fps_est < args.fps * 0.6:
            log("  [warn] 实测只有 %.1f fps（请求 %.0f fps）：这颗外接相机 720p 只有 ~9 fps、"
                "1080p 才有 30 fps；也可能 --camera 选成了笔记本内置（--list 看设备）"
                % (fps_est, args.fps))
        return cap, actual
    raise RuntimeError("摄像头打不开：%s。换个 --camera 0/1/2 或先关掉占用摄像头的程序。"
                       % (last_error or "未知原因"))


def camera_names(log=None):
    """设备友好名列表（DirectShow 枚举顺序；可选依赖 pygrabber，拿不到返回 None）。"""
    try:
        from pygrabber.dshow_graph import FilterGraph
    except Exception:
        return None
    try:
        return list(FilterGraph().get_input_devices())
    except Exception as exc:                      # COM 挂了也不该拖垮录制
        if log is not None:
            log("  [warn] pygrabber 枚举设备名失败：%s" % exc)
        return None


def probe_mode(index, width, height, fps, seconds=0.8, log=None):
    """按请求模式打开一次、量 0.8 s 真实帧率；返回 `(backend, 实测 fps, 协商尺寸)` 或 None。"""
    for name, api in backend_candidates("auto"):
        cap = cv2.VideoCapture(index, api)
        if not cap.isOpened():
            cap.release()
            continue
        cap.set(cv2.CAP_PROP_FOURCC, cv2.VideoWriter_fourcc(*"MJPG"))
        cap.set(cv2.CAP_PROP_FPS, float(fps))
        cap.set(cv2.CAP_PROP_FRAME_WIDTH, float(width))
        cap.set(cv2.CAP_PROP_FRAME_HEIGHT, float(height))
        for _ in range(8):
            cap.read()
        got_w = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
        got_h = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
        count = 0
        started = time.perf_counter()
        while time.perf_counter() - started < seconds:
            ok, _frame = cap.read()
            if not ok:
                break
            count += 1
        elapsed = max(1e-6, time.perf_counter() - started)
        cap.release()
        if count > 0:
            return name, count / elapsed, (got_w, got_h)
    return None


def list_cameras(args, log=None):
    """`--list`：列出本机摄像头、设备名（可选）、1080p/720p 实测帧率。"""
    if log is None:
        log = _console
    names = camera_names(log)
    if names is None:
        log("[list] 没装 pygrabber，无法显示设备名（pip install pygrabber 后可显示）；下面只有索引")
    log("[list] 逐个索引试开（MSMF 优先），每次量 0.8 s 真实帧率")
    found = False
    for index in range(5):
        name = names[index] if names and index < len(names) else "?"
        row = "  index %d  %-26s" % (index, name[:26])
        details = []
        for width, height in ((1920, 1080), (1280, 720)):
            got = probe_mode(index, width, height, args.fps, log=log)
            if got is None:
                continue
            backend, fps, size = got
            details.append("%s %dx%d %.1f fps" % (backend, size[0], size[1], fps))
        if details:
            found = True
            log(row + "  " + " | ".join(details))
    if not found:
        log("[list] 一个都打不开：检查 USB 连接、是否被别的程序占用")
    return 0


def open_writer(path, codec, fps, size, log):
    fourcc = cv2.VideoWriter_fourcc(*codec)
    writer = cv2.VideoWriter(path, fourcc, float(fps), size)
    if not writer.isOpened():
        raise RuntimeError("VideoWriter 打不开：%s（codec=%s）。换 --codec MJPG --ext avi 试试"
                           % (path, codec))
    log("  写盘编码：%s（%s）" % (codec, os.path.splitext(path)[1].lstrip(".")))
    return writer


# ---------------------------------------------------------------- 画面

def draw_ready(frame, actual, log_lines, capture_fps=None):
    canvas = frame.copy()
    cv2.putText(canvas, "READY  |  T: start recording    Q: quit",
                (12, 30), HUD_FONT, 0.62, HUD_GREEN, 2, cv2.LINE_AA)
    detail = "camera %d  %dx%d @ %.0f fps (%s)" % (
        actual.get("index", 0), actual["width"], actual["height"],
        actual["fps"], actual["backend"])
    if capture_fps:
        detail += "  capture ~%.1f fps" % capture_fps
    cv2.putText(canvas, detail,
                (12, frame.shape[0] - 14), HUD_FONT, 0.5, HUD_WHITE, 1, cv2.LINE_AA)
    for offset, text in enumerate(log_lines[-3:]):
        cv2.putText(canvas, text[:96], (12, 60 + 22 * offset),
                    HUD_FONT, 0.5, HUD_ORANGE, 1, cv2.LINE_AA)
    return canvas


def draw_recording(frame, elapsed, frames, output, write_fps=0.0):
    canvas = frame.copy()
    cv2.circle(canvas, (26, 26), 9, HUD_RED, -1, cv2.LINE_AA)
    cv2.putText(canvas, "REC %s  frames=%d" % (format_hms(elapsed), frames),
                (46, 34), HUD_FONT, 0.62, HUD_RED, 2, cv2.LINE_AA)
    note = os.path.basename(output or "")
    if write_fps:
        note += "   @ %.1f fps" % write_fps
    cv2.putText(canvas, note, (12, frame.shape[0] - 14),
                HUD_FONT, 0.5, HUD_WHITE, 1, cv2.LINE_AA)
    return canvas


# ---------------------------------------------------------------- 主流程

class Recorder(object):
    """一段录制：开写 → 逐帧写 → 收口 + 回读校验。状态都在这里，循环只管按键。"""

    def __init__(self, args, log):
        self.args = args
        self.log = log
        self.writer = None
        self.output = None
        self.saved = []
        self.started_at = None
        self.frames = 0
        self.write_fps = 0.0

    def start(self, frame, fps=None):
        height, width = frame.shape[:2]
        path = self.args.out or unique_path(
            self.args.dir,
            self.args.name or default_stem(),
            self.args.ext or CODEC_EXT.get(self.args.codec, "mp4"))
        if os.path.dirname(path):
            os.makedirs(os.path.dirname(path), exist_ok=True)
        write_fps = fps if fps and fps > 1 else (self.args.fps or 30.0)
        self.writer = open_writer(path, self.args.codec, write_fps,
                                  (width, height), self.log)
        self.write_fps = write_fps
        self.output = path
        self.started_at = time.time()
        self.frames = 0
        self.log("[rec] 开始录制 → %s（写盘 %.1f fps）" % (path, write_fps))
        return path

    def write(self, frame):
        if self.writer is None:
            return
        self.writer.write(frame)
        self.frames += 1

    def stop(self):
        """收口并回读；返回 `(ok, 摘要dict 或 None)`。"""
        if self.writer is None:
            return True, None
        self.writer.release()
        self.writer = None
        elapsed = time.time() - (self.started_at or time.time())
        ok, info = verify_recording(self.output)
        info["seconds"] = round(elapsed, 2)
        info["recorded_fps"] = round(self.frames / elapsed, 1) if elapsed > 0 else 0.0
        if ok:
            self.saved.append(info)
            drift = 0.0
            if self.write_fps > 1:
                drift = abs(info["recorded_fps"] - self.write_fps) / self.write_fps
            if drift > 0.15:
                self.log("[warn] 实际写入 %.1f fps，写盘头写的是 %.1f fps（差 %.0f%%）；"
                         "回放速度会漂，标注时按 capture.json 的 recorded_fps 复核"
                         % (info["recorded_fps"], self.write_fps, drift * 100))
            meta = {
                "schema": "sentinel/capture@1",
                "video": os.path.basename(info["path"]),
                "video_sha256": sha256_file(info["path"]),
                "fps": info["fps"],
                "write_fps": round(self.write_fps, 2),
                "width": info["width"],
                "height": info["height"],
                "frames": info["frames"],
                "recorded_fps": info["recorded_fps"],
                "duration_ms": int(round(elapsed * 1000)),
                "camera": self.args.camera,
                "fourcc": self.args.fourcc,
                "requested": {"width": self.args.width, "height": self.args.height,
                              "fps": self.args.fps},
                "captured_at_local": time.strftime("%Y-%m-%dT%H:%M:%S",
                                                   time.localtime(self.started_at or time.time())),
            }
            meta_path = os.path.splitext(info["path"])[0] + ".capture.json"
            try:
                import json
                with open(meta_path, "w", encoding="utf-8") as handle:
                    json.dump(meta, handle, ensure_ascii=False, indent=2)
                    handle.write("\n")
                self.log("[rec] 元数据 → %s" % meta_path)
            except OSError as exc:
                self.log("[warn] 元数据写失败：%s" % exc)
            self.log("[rec] 停止：%.1f s / %d 帧 / 实测 %.1f fps / %.1f MB"
                     % (elapsed, info["frames"], info["recorded_fps"],
                        info["size_bytes"] / 1048576.0))
        else:
            self.log("[rec] 失败：%s（文件 %s）" % (info["problem"], self.output))
        return ok, info


def parse_args(argv=None):
    parser = argparse.ArgumentParser(
        description="门口素材录制：预览 → T 开始 → C 停止（输出在 datasets/scenarios/）")
    parser.add_argument("--camera", type=int, default=1,
                        help="摄像头序号（默认 1 = 外接 HD USB Camera；0 是笔记本内置）")
    parser.add_argument("--width", type=int, default=1920,
                        help="请求宽（默认 1920；这颗外接相机 720p 只有 ~9 fps）")
    parser.add_argument("--height", type=int, default=1080,
                        help="请求高（默认 1080；720p 只有 ~9 fps）")
    parser.add_argument("--fps", type=float, default=30.0, help="请求帧率（默认 30）")
    parser.add_argument("--fourcc", default="MJPG",
                        help="采集 FOURCC（默认 MJPG；YUYV 会大幅降帧率）")
    parser.add_argument("--backend", default="auto",
                        choices=("auto", "dshow", "msmf", "any"),
                        help="采集后端（默认 auto：Windows 先 MSMF 再 DSHOW）")
    parser.add_argument("--list", action="store_true",
                        help="只列出本机摄像头及其实测分辨率/帧率，不录制")
    parser.add_argument("--dir", default=DEFAULT_DIR, help="输出目录（默认 datasets/scenarios）")
    parser.add_argument("--name", default=None,
                        help="文件名主干（如 sc01_replacement）；默认 rec_<时间戳>")
    parser.add_argument("--out", default=None, help="直接指定完整输出路径（优先于 --name）")
    parser.add_argument("--codec", default=DEFAULT_CODEC,
                        help="写盘编码（默认 mp4v；可选 MJPG/avc1/XVID）")
    parser.add_argument("--ext", default=None, help="扩展名（默认按 codec 推）")
    parser.add_argument("--seconds", type=float, default=0.0,
                        help="自动停止秒数（0=手动按 C；headless 必填）")
    parser.add_argument("--headless", action="store_true",
                        help="不弹窗、不给按键（无人值守自检用；自动开录）")
    args = parser.parse_args(argv)
    if args.headless and args.seconds <= 0:
        parser.error("--headless 必须配 --seconds（自动停止秒数）")
    return args


def _console(line):
    """控制台日志：显式 flush，重定向到文件/管道时也能立刻看到（排障用）。"""
    print(line, flush=True)


def run(args, log=_console):
    cap, actual = open_camera(args, log)
    actual["index"] = args.camera
    log("[cam] 后端=%s 实际 %dx%d @ %.0f fps（请求 %dx%d @ %.0f）" % (
        actual["backend"], actual["width"], actual["height"], actual["fps"],
        args.width, args.height, args.fps))
    recorder = Recorder(args, log)
    display_lines = []
    ok_all = True
    try:
        if args.headless:
            fps_est, frame = measure_capture_fps(cap, count=30, discard=15, log=log)
            if frame is None:
                raise RuntimeError("读不到帧")
            recorder.start(frame, sane_write_fps(fps_est, args.fps))
            log("[rec] 实测采集 %.1f fps" % (fps_est or 0.0))
            deadline = time.time() + args.seconds
            while time.time() < deadline:
                ok, frame = cap.read()
                if not ok:
                    log("[warn] 录制中丢帧/断流")
                    break
                recorder.write(frame)
            ok, _info = recorder.stop()
            ok_all = ok_all and ok
            return 0 if ok_all else 1

        window = "Sentinel Recorder  [T] start  [C] stop  [Q] quit"
        cv2.namedWindow(window, cv2.WINDOW_AUTOSIZE)
        log("按键：T 开始录制 / C 停止 / Q 或 Esc 退出（窗口要有焦点）")
        stamps = []

        def after_stop(ok_now, info):
            """收口后把结果同时写进控制台与预览画面（预览画面里能立刻看到文件名）。"""
            mark = "✓" if ok_now else "✗ 录制失败：%s" % info["problem"]
            display_lines.append("saved %s  %s" % (os.path.basename(info["path"]), mark))
            return ok_now

        while True:
            ok, frame = cap.read()
            if not ok or frame is None:
                log("[warn] 摄像头读帧失败，退出")
                ok_all = False
                break
            stamps.append(time.time())

            if recorder.writer is not None:
                recorder.write(frame)
                elapsed = time.time() - (recorder.started_at or time.time())
                if args.seconds > 0 and elapsed >= args.seconds:
                    ok_now, info = recorder.stop()
                    ok_all = ok_all and after_stop(ok_now, info)
                shown = draw_recording(frame, elapsed, recorder.frames,
                                       recorder.output, recorder.write_fps)
            else:
                shown = draw_ready(frame, actual, display_lines, estimate_fps(stamps))

            cv2.imshow(window, shown)
            key = cv2.waitKey(1) & 0xFF
            if key in (ord("q"), 27):
                log("[rec] 退出")
                break
            if key in (ord("t"), ord("T")):
                if recorder.writer is None:
                    recorder.start(frame, sane_write_fps(
                        estimate_fps(stamps, 60), args.fps))
                else:
                    log("[rec] 已在录制中（按 C 停止）")
            elif key in (ord("c"), ord("C")):
                if recorder.writer is not None:
                    ok_now, info = recorder.stop()
                    ok_all = ok_all and after_stop(ok_now, info)
                else:
                    log("[rec] 还没开始录制")
        # 退出时还在录：先收口，别留坏文件
        if recorder.writer is not None:
            ok_now, _info = recorder.stop()
            ok_all = ok_all and ok_now
    finally:
        cap.release()
        cv2.destroyAllWindows()
    return 0 if ok_all else 1


def main(argv=None):
    args = parse_args(argv)
    try:
        if args.list:
            return list_cameras(args)
        return run(args)
    except RuntimeError as exc:
        print("[错误] %s" % exc, file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())