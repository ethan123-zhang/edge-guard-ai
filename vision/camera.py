"""视频源抽象：USB 摄像头 / 视频文件 / 图片目录。

三件事必须做到：
1. **摄像头打不开要在启动时报错**，别等主循环里静默 0 帧
2. `read()` 返回**帧时刻**（不是处理时刻）—— `face_event.frame_ts` 要用它
3. 板端和 PC 的差异（V4L2 vs DirectShow、是否有显示器）只在这层和 profile 里体现
"""
import os
import time

import cv2

from vision.events import now_ms
from vision.imageio import imread

_BACKENDS = {
    "auto": cv2.CAP_ANY,
    "any": cv2.CAP_ANY,
    "dshow": cv2.CAP_DSHOW,
    "msmf": cv2.CAP_MSMF,
    "v4l2": cv2.CAP_V4L2,
}

_IMAGE_EXTS = (".jpg", ".jpeg", ".png", ".bmp", ".webp")


class SourceError(RuntimeError):
    """视频源打不开。消息里要带出试过什么，现场排障全靠它。"""


class FrameSource(object):
    """所有源的共同接口。"""

    def open(self):
        raise NotImplementedError

    def read(self):
        """返回 `(ok, frame_bgr, frame_ts_ms)`。"""
        raise NotImplementedError

    def release(self):
        pass

    def describe(self):
        return self.__class__.__name__


class OpenCVCamera(FrameSource):
    def __init__(self, device=0, width=1280, height=720, backend="auto",
                 mirror=False, warmup_frames=3):
        self.device = int(device)
        self.width = int(width)
        self.height = int(height)
        self.backend = str(backend)
        self.mirror = bool(mirror)
        self.warmup_frames = int(warmup_frames)
        self._cap = None

    def open(self):
        api = _BACKENDS.get(self.backend, cv2.CAP_ANY)
        cap = cv2.VideoCapture(self.device, api)
        used = self.backend
        if not cap.isOpened() and api != cv2.CAP_ANY:
            # DirectShow 在部分 USB 摄像头上会拒绝按序号打开，退回自动探测（2026-10-23 实测本机如此）
            cap.release()
            cap = cv2.VideoCapture(self.device, cv2.CAP_ANY)
            used = "auto(回退)"
        if not cap.isOpened():
            raise SourceError(
                "摄像头打不开：device=%d backend=%s。"
                "排查顺序：① 设备管理器里有没有这个摄像头 ② 别的程序（微信/浏览器）是不是占着 "
                "③ 换 backend（dshow/msmf/auto）④ 换 device 序号" % (self.device, self.backend)
            )
        cap.set(cv2.CAP_PROP_FRAME_WIDTH, self.width)
        cap.set(cv2.CAP_PROP_FRAME_HEIGHT, self.height)
        cap.set(cv2.CAP_PROP_BUFFERSIZE, 1)
        self._cap = cap
        self._used_backend = used
        for _ in range(self.warmup_frames):
            cap.read()
        return self

    def read(self):
        ok, frame = self._cap.read()
        if not ok or frame is None:
            return False, None, now_ms()
        if self.mirror:
            frame = cv2.flip(frame, 1)
        return True, frame, now_ms()

    def release(self):
        if self._cap is not None:
            self._cap.release()
            self._cap = None

    def describe(self):
        return "camera device=%d %dx%d backend=%s" % (
            self.device, self.width, self.height, getattr(self, "_used_backend", self.backend)
        )


class VideoFileSource(FrameSource):
    """回放视频（演示兜底 / 没有摄像头时用）。循环播放。"""

    def __init__(self, path, mirror=False, loop=True):
        self.path = str(path)
        self.mirror = bool(mirror)
        self.loop = bool(loop)
        self._cap = None

    def open(self):
        if not os.path.exists(self.path):
            raise SourceError("视频文件不存在：%s" % self.path)
        cap = cv2.VideoCapture(self.path)
        if not cap.isOpened():
            raise SourceError("视频打不开：%s（编码不被 OpenCV 支持？换 mp4/H.264）" % self.path)
        self._cap = cap
        return self

    def read(self):
        ok, frame = self._cap.read()
        if not ok or frame is None:
            if not self.loop:
                return False, None, now_ms()
            self._cap.set(cv2.CAP_PROP_POS_FRAMES, 0)
            ok, frame = self._cap.read()
            if not ok or frame is None:
                return False, None, now_ms()
        if self.mirror:
            frame = cv2.flip(frame, 1)
        return True, frame, now_ms()

    def release(self):
        if self._cap is not None:
            self._cap.release()
            self._cap = None

    def describe(self):
        return "video %s (loop=%s)" % (self.path, self.loop)


class ImageDirSource(FrameSource):
    """图片目录轮播：没摄像头时把静态测试图当数据流喂进管线。"""

    def __init__(self, directory, mirror=False, fps=2.0):
        self.directory = str(directory)
        self.mirror = bool(mirror)
        self.fps = float(fps)
        self._files = []
        self._idx = 0
        self._last = 0.0

    def open(self):
        if not os.path.isdir(self.directory):
            raise SourceError("图片目录不存在：%s" % self.directory)
        self._files = sorted(
            os.path.join(self.directory, name)
            for name in os.listdir(self.directory)
            if name.lower().endswith(_IMAGE_EXTS)
        )
        if not self._files:
            raise SourceError("图片目录里没有可读图片：%s" % self.directory)
        return self

    def read(self):
        now = time.time()
        if self.fps > 0 and (now - self._last) < (1.0 / self.fps):
            time.sleep(max(0.0, (1.0 / self.fps) - (now - self._last)))
        self._last = time.time()
        path = self._files[self._idx % len(self._files)]
        self._idx += 1
        frame = imread(path)
        if frame is None:
            return False, None, now_ms()
        if self.mirror:
            frame = cv2.flip(frame, 1)
        return True, frame, now_ms()

    def describe(self):
        return "image-dir %s (%d 张)" % (self.directory, len(self._files))


def create_source(cfg):
    """按配置造源：`camera.source` 是 int → 摄像头；是目录 → 图片轮播；是文件 → 视频。"""
    cam = cfg["camera"]
    spec = cam["source"]
    root = str(spec)

    if isinstance(spec, int) or (isinstance(root, str) and root.isdigit()):
        return OpenCVCamera(
            device=int(spec), width=cam["width"], height=cam["height"],
            backend=cam["backend"], mirror=cam["mirror"],
            warmup_frames=cam["warmup_frames"],
        )
    if os.path.isdir(root):
        return ImageDirSource(root, mirror=cam["mirror"])
    return VideoFileSource(root, mirror=cam["mirror"])
