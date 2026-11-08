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

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:                    # 让 `py scripts/record_camera.py` 也能 import vision
    sys.path.insert(0, ROOT)

import cv2

DEFAULT_DIR = os.path.join("datasets", "scenarios")
DEFAULT_CODEC = "mp4v"
CODEC_EXT = {"mp4v": "mp4", "avc1": "mp4", "h264": "mp4", "XVID": "avi", "MJPG": "avi"}
HUD_FONT = cv2.FONT_HERSHEY_SIMPLEX
HUD_ORANGE = (0, 165, 255)
HUD_GREEN = (0, 220, 0)
HUD_RED = (0, 0, 255)
HUD_WHITE = (255, 255, 255)
DETECT_WIDTH = 640        # --detect 的检测输入宽度（整帧 1080p 送 YuNet 要 120 ms/帧）


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


def face_summary(faces):
    """`(人数, 最大人脸宽 px)`；给 `--detect` 的 HUD 用。faces 元素要有 `.size`。"""
    if not faces:
        return 0, 0
    return len(faces), max(int(face.size) for face in faces)


def detect_scaled(face_detector, frame, detect_width=DETECT_WIDTH):
    """先缩到 `detect_width` 宽再检测，坐标按比例放大回原帧。

    为什么要缩：YuNet 的 `detect()` 会把**整帧**设为输入尺寸，1920×1080 直接送进去
    实测 **120 ms/帧**，30 fps 的录制循环立刻被拖到 20.7 fps（2026-11-08 实测）；
    缩到 640 宽后是 ~15 ms，且人脸在 640 宽下仍有 50 px 以上，检出率不受影响。
    """
    height, width = frame.shape[:2]
    if width <= detect_width:
        return face_detector.detect(frame)
    from vision.detector import FaceDetection     # 只有 --detect 才需要 vision 包

    scale = float(width) / float(detect_width)
    small = cv2.resize(frame, (int(detect_width),
                               max(1, int(round(height / scale)))))
    faces = []
    for face in face_detector.detect(small):
        faces.append(FaceDetection(face.x * scale, face.y * scale,
                                   face.w * scale, face.h * scale, face.score))
    return faces


def timing_ratio(frames, elapsed, write_fps):
    """实际采集帧率 ÷ 写盘帧率。明显偏离 1 = 文件回放速度不对（快/慢）。"""
    if not frames or not elapsed or not write_fps:
        return None
    return (float(frames) / float(elapsed)) / float(write_fps)


def needs_retime(frames, elapsed, write_fps, tolerance=0.10):
    """回放速度偏离超过 `tolerance` 就必须按实际帧率重写容器。"""
    ratio = timing_ratio(frames, elapsed, write_fps)
    return ratio is not None and abs(ratio - 1.0) > float(tolerance)


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


def retime_video(path, target_fps, log=None):
    """把已写好的视频按 `target_fps` 重写一遍容器，让回放速度回到实时。

    为什么会用到：录制循环被拖慢时（本机 `--detect` 曾把 30 fps 拖到 20.7 fps），
    写盘头仍写着 30 fps，回放就快 1.45 倍。帧内容没问题，错的只是 fps 头，
    重编码一次即可（mp4v 1080p 约 11 ms/帧）；原文件留成 `<name>.pre-retime.mp4`。
    返回 `(ok, 帧数)`。
    """
    log = log or (lambda msg: None)
    cap = cv2.VideoCapture(path)
    if not cap.isOpened():
        return False, 0
    width = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH) or 0)
    height = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT) or 0)
    ext = os.path.splitext(path)[1] or ".mp4"
    temp = "%s.retiming%s" % (os.path.splitext(path)[0], ext)
    writer = cv2.VideoWriter(temp, cv2.VideoWriter_fourcc(*"mp4v"),
                             float(target_fps), (width, height))
    if width <= 0 or height <= 0 or not writer.isOpened():
        cap.release()
        return False, 0
    frames = 0
    while True:
        ok, frame = cap.read()
        if not ok:
            break
        writer.write(frame)
        frames += 1
    writer.release()
    cap.release()
    if frames <= 0:
        try:
            os.remove(temp)
        except OSError:
            pass
        return False, 0
    backup = "%s.pre-retime%s" % (os.path.splitext(path)[0], ext)
    try:
        if os.path.exists(backup):
            os.remove(backup)
        os.replace(path, backup)
        os.replace(temp, path)
    except OSError as exc:
        log("[warn] 重写容器失败：%s" % exc)
        return False, 0
    log("[rec] 已按 %.1f fps 重写容器（原文件保留为 %s）"
        % (float(target_fps), os.path.basename(backup)))
    return True, frames

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


def load_face_detector(log):
    """`--detect` 用的 YuNet 检测器（懒加载，默认不装）。

    本机有个坑：`cv2` 的 ONNX 导入器读不了带中文的路径（项目目录叫「哨兵管家」），
    所以模型路径含非 ASCII 时先拷到系统临时目录再加载。
    """
    import shutil
    import tempfile

    from vision import config, detector

    cfg = config.load("sim")
    model = str(config.resolve_vision(cfg["detection"]["model"]))
    if not os.path.exists(model):
        raise RuntimeError("找不到人脸检测模型：%s（先补 vision/models/）" % model)
    if any(ord(ch) >= 128 for ch in model):
        ascii_dir = os.path.join(tempfile.gettempdir(), "sentinel-models")
        os.makedirs(ascii_dir, exist_ok=True)
        target = os.path.join(ascii_dir, os.path.basename(model))
        if not os.path.exists(target) or os.path.getsize(target) != os.path.getsize(model):
            shutil.copyfile(model, target)
        cfg["detection"]["model"] = target
        model = target
    face_detector = detector.create_detector(cfg)
    log("[detect] 人脸叠加：%s（每 %d 帧检测一次；只画在预览上，不录进视频）"
        % (face_detector.describe(), 5))
    return face_detector


def open_writer(path, codec, fps, size, log):
    fourcc = cv2.VideoWriter_fourcc(*codec)
    writer = cv2.VideoWriter(path, fourcc, float(fps), size)
    if not writer.isOpened():
        raise RuntimeError("VideoWriter 打不开：%s（codec=%s）。换 --codec MJPG --ext avi 试试"
                           % (path, codec))
    log("  写盘编码：%s（%s）" % (codec, os.path.splitext(path)[1].lstrip(".")))
    return writer


# ---------------------------------------------------------------- 画面

def draw_faces(canvas, faces, min_face_px=100):
    """把人脸框画在预览画面上（只画在显示副本上，录进去的仍是干净原帧）。"""
    if not faces:
        return
    for face in faces:
        good = int(face.size) >= min_face_px
        color = (0, 220, 0) if good else (0, 165, 255)
        cv2.rectangle(canvas, (face.x, face.y), (face.x + face.w, face.y + face.h),
                      color, 2)
        cv2.putText(canvas, "%dpx %.2f" % (int(face.size), face.score),
                    (face.x, max(22, face.y - 6)), HUD_FONT, 0.55, color, 2, cv2.LINE_AA)


def draw_ready(frame, actual, log_lines, capture_fps=None, faces=None):
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
    if faces is not None:
        draw_faces(canvas, faces)
        count, widest = face_summary(faces)
        cv2.putText(canvas, "faces=%d  max=%dpx" % (count, widest),
                    (frame.shape[1] - 260, frame.shape[0] - 14),
                    HUD_FONT, 0.6, HUD_GREEN if count == 1 and widest >= 100
                    else HUD_ORANGE, 2, cv2.LINE_AA)
    for offset, text in enumerate(log_lines[-3:]):
        cv2.putText(canvas, text[:96], (12, 60 + 22 * offset),
                    HUD_FONT, 0.5, HUD_ORANGE, 1, cv2.LINE_AA)
    return canvas


def draw_recording(frame, elapsed, frames, output, write_fps=0.0, faces=None):
    canvas = frame.copy()
    cv2.circle(canvas, (26, 26), 9, HUD_RED, -1, cv2.LINE_AA)
    cv2.putText(canvas, "REC %s  frames=%d" % (format_hms(elapsed), frames),
                (46, 34), HUD_FONT, 0.62, HUD_RED, 2, cv2.LINE_AA)
    note = os.path.basename(output or "")
    if write_fps:
        note += "   @ %.1f fps" % write_fps
    cv2.putText(canvas, note, (12, frame.shape[0] - 14),
                HUD_FONT, 0.5, HUD_WHITE, 1, cv2.LINE_AA)
    if faces is not None:
        draw_faces(canvas, faces)
        count, widest = face_summary(faces)
        cv2.putText(canvas, "faces=%d  max=%dpx" % (count, widest),
                    (frame.shape[1] - 260, frame.shape[0] - 14),
                    HUD_FONT, 0.6, HUD_GREEN if count == 1 and widest >= 100
                    else HUD_ORANGE, 2, cv2.LINE_AA)
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
        """收口、回读校验、必要时按实际帧率重写容器；返回 `(ok, 摘要dict)`。"""
        if self.writer is None:
            return True, None
        self.writer.release()
        self.writer = None
        elapsed = time.time() - (self.started_at or time.time())
        ok, info = verify_recording(self.output)
        capture_fps = round(self.frames / elapsed, 1) if elapsed > 0 else 0.0
        retimed = False
        if ok and not self.args.no_retime and needs_retime(self.frames, elapsed,
                                                           self.write_fps):
            ratio = timing_ratio(self.frames, elapsed, self.write_fps) or 1.0
            self.log("[rec] 回放速度偏 %.0f%%（实际 %.1f fps，写盘头 %.1f fps），"
                     "按实际帧率重写容器" % ((ratio - 1.0) * 100, capture_fps,
                                             self.write_fps))
            retimed_ok, retimed_frames = retime_video(info["path"], capture_fps,
                                                      self.log)
            if retimed_ok and retimed_frames == self.frames:
                retimed = True
                self.write_fps = capture_fps
                ok2, info2 = verify_recording(info["path"])
                if ok2:
                    ok, info = ok2, info2
                else:
                    self.log("[warn] 重写后回读失败：%s" % info2["problem"])
            elif retimed_ok:
                self.log("[warn] 重写后帧数变了（%d → %d），保留原文件"
                         % (self.frames, retimed_frames))
        info["seconds"] = round(elapsed, 2)
        info["capture_fps"] = capture_fps
        info["recorded_fps"] = capture_fps
        if ok:
            self.saved.append(info)
            ratio = timing_ratio(self.frames, elapsed, self.write_fps)
            if not retimed and ratio is not None and abs(ratio - 1.0) > 0.10:
                self.log("[warn] 回放速度仍偏 %.0f%%（实际 %.1f fps vs 写盘 %.1f fps）："
                         "标注时按 capture.json 的 capture_fps 换算时间"
                         % ((ratio - 1.0) * 100, capture_fps, self.write_fps))
            meta = {
                "schema": "sentinel/capture@1",
                "video": os.path.basename(info["path"]),
                "video_sha256": sha256_file(info["path"]),
                "fps": info["fps"],
                "write_fps": round(self.write_fps, 2),
                "capture_fps": capture_fps,
                "retimed": retimed,
                "width": info["width"],
                "height": info["height"],
                "frames": info["frames"],
                "recorded_fps": capture_fps,
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
    parser.add_argument("--detect", action="store_true",
                        help="预览上叠加人脸框/宽度/同框人数（不录进视频）")
    parser.add_argument("--detect-every", type=int, default=5,
                        help="--detect 每几帧检测一次（默认 5，兼顾 30 fps）")
    parser.add_argument("--detect-width", type=int, default=DETECT_WIDTH,
                        help="--detect 的检测输入宽度（默认 640；整帧 1080p 送 YuNet 要 120 ms/帧）")
    parser.add_argument("--no-retime", action="store_true",
                        help="关闭收口时的自动重写容器（默认开启：回放速度偏 >10%% 时按实际帧率重写）")
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
    face_detector = load_face_detector(log) if (args.detect and not args.headless) else None
    if args.detect and args.headless:
        log("  [warn] --headless 下没有窗口，--detect 被忽略")
    faces = [] if face_detector is not None else None
    frame_index = 0
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
            frame_index += 1
            if face_detector is not None and frame_index % max(1, args.detect_every) == 0:
                try:
                    faces = detect_scaled(face_detector, frame, args.detect_width)
                except Exception as exc:          # 检测失败不该毁掉录制
                    faces = []
                    log("[warn] 人脸检测失败（继续录）：%s" % exc)

            if recorder.writer is not None:
                recorder.write(frame)
                elapsed = time.time() - (recorder.started_at or time.time())
                if args.seconds > 0 and elapsed >= args.seconds:
                    ok_now, info = recorder.stop()
                    ok_all = ok_all and after_stop(ok_now, info)
                shown = draw_recording(frame, elapsed, recorder.frames,
                                       recorder.output, recorder.write_fps, faces)
            else:
                shown = draw_ready(frame, actual, display_lines,
                                   estimate_fps(stamps), faces)

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