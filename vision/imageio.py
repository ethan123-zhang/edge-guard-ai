"""Unicode 安全的图片读写。

**为什么需要这一层**：Windows 上 `cv2.imread/imwrite` 走 ANSI 代码页，
仓库路径 `E:\\哨兵管家\\...` 里的中文会让 `imread` 直接返回 None、
`imwrite` 返回 False —— 2026-10-23 本机实测踩到（截图写不出来、照片读不进去）。
板端是 Linux（UTF-8），用这层也没有副作用。
"""
import os

import cv2
import numpy as np


def imread(path, flags=cv2.IMREAD_COLOR):
    """读图；失败返回 None（与 cv2.imread 语义一致，但不挑路径编码）。"""
    try:
        data = np.fromfile(path, dtype=np.uint8)
    except OSError:
        return None
    if data.size == 0:
        return None
    return cv2.imdecode(data, flags)


def imwrite(path, image, jpeg_quality=90):
    """写图；返回 True/False。按扩展名选编码，默认 JPEG。"""
    ext = os.path.splitext(str(path))[1] or ".jpg"
    params = []
    if ext.lower() in (".jpg", ".jpeg"):
        params = [int(cv2.IMWRITE_JPEG_QUALITY), int(jpeg_quality)]
    ok, buf = cv2.imencode(ext, image, params)
    if not ok:
        return False
    try:
        with open(str(path), "wb") as fh:
            fh.write(buf.tobytes())
    except OSError:
        return False
    return True
