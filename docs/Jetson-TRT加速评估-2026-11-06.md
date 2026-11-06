# Jetson TensorRT 加速评估（2026-11-06）

## 问题

板端 OpenCV 4.1.1 是 Ubuntu 发行版包，**编译时没带 CUDA**：`cv2.cuda.getCudaEnabledDeviceCount()=0`，build info 里没有 CUDA 段。所以检测和识别一直走 CPU。CUDA 10.2 与 TensorRT 8.2.1 其实都在板子上（`/usr/src/tensorrt/bin/trtexec`、pycuda 2022.1 可用），只是没用上。

## 做法（只读，不改生产）

1. 同一段固定视频采样 40 帧，先量 CPU 分段耗时（blob、检测前向、解码、对齐、SFace 前向）。
2. 用 `trtexec` 把两个现有 ONNX 模型各转 FP32 / FP16 两个 engine。
3. 用 pycuda 跑 engine，和 `cv2.dnn` 同一输入的输出做逐值对比。
4. 同一输入重复 30–50 次取中位耗时。

## 结果

| 阶段（板端中位） | CPU dnn | TRT FP32 | TRT FP16 | 加速（FP16） |
| --- | --- | --- | --- | --- |
| 检测前向（320×320） | 84.0 ms | 15.2 ms | 11.2 ms | 7.5× |
| SFace 前向（112×112） | 219.0 ms/帧（该帧所有脸） | 22.5 ms/脸 | 18.1 ms/脸 | 12.1× |
| 其它（blob + 解码 + 对齐） | 5.9 ms | — | 5.9 ms（仍在 CPU） | — |
| **合计/采样帧** | **≈ 309 ms** | — | **≈ 35 ms**（单脸） | **≈ 8.8×** |

正确性：YuNet 输出与 CPU 的最大绝对差 7.5e-6（FP32/FP16 都是）；SFace 余弦 1.0000（FP32）、0.99999（FP16），FP16 最大绝对差 0.0113。

## 两个必须知道的坑

1. **ONNX 声明的输入尺寸和实际不一致**：YuNet 实验副本声明 `1×3×120×160`，但管线一直喂 320×320（全卷积，cv2 不校验）。TRT 会按声明建静态 engine（120×160），喂 320 直接报维度越界。做法是只改声明维度生成 `yunet_2022_opset9_trt320.onnx`（checker 通过，数值等价见上表），原模型不动。
2. **pycuda 里不能只存 int 指针**：把 `cuda.mem_alloc` 的返回值转成 `int` 存下来，`DeviceAllocation` 对象会被 GC，device 缓冲随之释放；之后 `memcpy_htod_async` 报 `invalid argument`（指针已失效）。必须同时保留对象本身。

## 结论与下一步

- 结论（实测）：这条链路用 TensorRT FP16 能把单帧推理从 ~309 ms 压到 ~35 ms（单脸），证明**不是板子算不动，是软件栈没启用**。这是离线基准，未接生产。
- 要进生产还需要：Engine 封装（binding、动态尺寸、多脸批）、FP16 数值回归（同视频重复率/余弦不变）、失败回退 CPU、engine 与模型哈希绑定。
- 不推荐重编 OpenCV with CUDA：Nano 上编译数小时，dnn CUDA 后端对小模型收益不如 TRT。
- 真实收益取决于业务流程是否每帧都跑识别；若仍按"新轨/高质量脸才识别"，CPU 也能接受，TRT 主要改善实时性余量。

证据：`docs/data/trt-acceleration-2026-11-06.json`；脚本在忽略目录 `logs/video-validation/trt_bench.py`、`trt_check.py`。