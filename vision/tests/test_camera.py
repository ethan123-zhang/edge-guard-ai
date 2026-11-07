"""摄像头采集参数测试：FOURCC/帧率必须真的设下去，且顺序对（否则 UVC 带宽协商不生效）。

为什么要有这一层：2026-11-07 板端实测发现，1280x720 不指定 FOURCC 时 V4L2 会协商成
YUYV，USB2 上采集只有 9.9 fps；显式 MJPG 后同一台相机采集 49.6 fps。这个差异完全
由"有没有把 FOURCC 设下去"决定，所以用假 VideoCapture 把它钉死在单测里（不碰真硬件）。
"""
import unittest

try:
    from unittest import mock
except ImportError:  # pragma: no cover - Python 3.6 自带 mock，这里只是兜底
    import mock

from vision import camera, config


class _FakeCapture(object):
    """记录 set() 调用顺序的假 VideoCapture。"""

    def __init__(self, device=0, api=0, readable=True):
        self.device = device
        self.api = api
        self.readable = readable
        self.set_calls = []
        self.read_calls = 0
        self.released = False

    def isOpened(self):
        return True

    def set(self, prop, value):
        self.set_calls.append((prop, value))
        return True

    def read(self):
        self.read_calls += 1
        if not self.readable:
            return (False, None)
        return (True, object())

    def release(self):
        self.released = True


def _patch_capture(store, readable=True):
    def factory(device, api=0):
        cap = _FakeCapture(device, api, readable=readable)
        store.append(cap)
        return cap
    return mock.patch.object(camera.cv2, "VideoCapture", side_effect=factory)


class TestOpenCVCameraFormat(unittest.TestCase):
    def test_sets_fourcc_and_fps_before_resolution(self):
        caps = []
        cam = camera.OpenCVCamera(device=0, width=1280, height=720, backend="v4l2",
                                  fourcc="MJPG", fps=30, warmup_frames=0)
        with _patch_capture(caps):
            cam.open()
        props = [prop for prop, _ in caps[0].set_calls]
        self.assertEqual(props, [
            camera.cv2.CAP_PROP_FOURCC, camera.cv2.CAP_PROP_FPS,
            camera.cv2.CAP_PROP_FRAME_WIDTH, camera.cv2.CAP_PROP_FRAME_HEIGHT,
            camera.cv2.CAP_PROP_BUFFERSIZE,
        ])
        self.assertEqual(caps[0].set_calls[0][1],
                         camera.cv2.VideoWriter_fourcc(*"MJPG"))
        self.assertEqual(caps[0].set_calls[1][1], 30)

    def test_unspecified_format_leaves_negotiation_alone(self):
        """留空 = 不碰 FOURCC/FPS，保持旧行为（PC 仿真 / 不支持格式的平台）。"""
        caps = []
        cam = camera.OpenCVCamera(device=0, width=640, height=480, backend="dshow",
                                  warmup_frames=0)
        with _patch_capture(caps):
            cam.open()
        props = [prop for prop, _ in caps[0].set_calls]
        self.assertNotIn(camera.cv2.CAP_PROP_FOURCC, props)
        self.assertNotIn(camera.cv2.CAP_PROP_FPS, props)

    def test_describe_reports_requested_format(self):
        cam = camera.OpenCVCamera(device=0, width=1280, height=720,
                                  fourcc="MJPG", fps=30)
        text = cam.describe()
        self.assertIn("MJPG", text)
        self.assertIn("fps=30", text)

    def test_fourcc_is_passed_through_create_source(self):
        cfg = config.load("sim")
        cfg["camera"]["fourcc"] = "MJPG"
        cfg["camera"]["fps"] = 30
        src = camera.create_source(cfg)
        self.assertIsInstance(src, camera.OpenCVCamera)
        self.assertEqual(src.fourcc, "MJPG")
        self.assertEqual(src.fps, 30.0)


class TestOpenFailureDiagnosis(unittest.TestCase):
    """打开了却读不到帧时要当场指出后端问题，而不是让主循环静默丢 50 帧。

    板端实测（2026-11-07）：backend=auto + FOURCC=MJPG 时 CAP_ANY 落到 GStreamer 后端，
    GStreamer 不认 FOURCC，连读 50 帧全失败；改成 backend=v4l2 立刻正常 16.9 fps。
    """

    def test_format_requested_but_unreadable_hints_v4l2(self):
        caps = []
        cam = camera.OpenCVCamera(device=0, width=1280, height=720, backend="auto",
                                  fourcc="MJPG", fps=30, warmup_frames=3)
        with _patch_capture(caps, readable=False):
            with self.assertRaises(camera.SourceError) as ctx:
                cam.open()
        self.assertIn("v4l2", str(ctx.exception))
        self.assertIn("MJPG", str(ctx.exception))
        self.assertTrue(caps[0].released, "报错前要把设备放掉，不能把摄像头占住")

    def test_unreadable_without_format_reports_hardware(self):
        caps = []
        cam = camera.OpenCVCamera(device=1, width=640, height=480, backend="dshow",
                                  warmup_frames=2)
        with _patch_capture(caps, readable=False):
            with self.assertRaises(camera.SourceError) as ctx:
                cam.open()
        self.assertIn("device=1", str(ctx.exception))

    def test_readable_camera_passes_warmup(self):
        caps = []
        cam = camera.OpenCVCamera(device=0, width=1280, height=720, backend="v4l2",
                                  fourcc="MJPG", fps=30, warmup_frames=3)
        with _patch_capture(caps):
            self.assertIs(cam.open(), cam)
        self.assertEqual(caps[0].read_calls, 3)


if __name__ == "__main__":
    unittest.main()