"""视觉节点 —— 将来原样跑在 Jetson Nano B01 上，现在先在 PC 上跑仿真。

板端约束（决定了本包怎么写）：
- Ubuntu 18.04 + **Python 3.6**：禁用 dataclasses / `from __future__ import annotations`
- OpenCV 4.5.4（自编译，CPU 版）：YuNet + SFace 都在；没有 CUDA
- 摄像头是 USB 1080p，不是 CSI

双后端设计：检测器 `yunet`（准）或 `haar`（离线兜底），识别器 `sface` 或 `none`。
模型文件不在时自动降级，**不会因为没下模型就跑不起来**。
"""

__version__ = "0.1.0"
