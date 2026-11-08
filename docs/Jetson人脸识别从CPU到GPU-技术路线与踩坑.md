# Jetson 人脸识别从 CPU 到 GPU：技术路线与踩坑

> 面向已学过 OpenCV、YOLO 和深度学习基础，但想弄清项目实际如何接线的读者。依据本仓库截至 2026-11-08 的代码与历史板端记录整理；当日另有一次 **SFace engine 重建实测**（无 sidecar → 内置 profile → trtexec → sidecar 复验，483 s，[证据](data/sface-engine-rebuild-2026-11-08.json)）。这里的板子是 **Jetson Nano**，不要把其他 Jetson 的性能或 TensorRT 新版本特性直接套进来。

## 一句话抓住整个改动

原先的正式服务在板端会退到 Haar 检测，缺少人脸五点，无法可靠地走完 SFace 对齐识别。即便在隔离实验里用 OpenCV `cv2.dnn` 跑通 YuNet、SFace，模型前向计算仍由 CPU 执行。后来项目把 **YuNet 找脸**和 **SFace 提特征**这两次模型前向交给 TensorRT 在 GPU 上执行，再把结果交还给 CPU 做解码、对齐、身份比较和事件处理。

这件事包含两项独立工作：**让完整识别管线能在旧版 OpenCV 的板子上运行**，以及**把其中的模型计算加速**。不能把“从 Haar 换成 YuNet”与“同一个 YuNet 从 CPU 换到 GPU”的效果混为一谈。

```text
摄像头帧
  → CPU 整理为 YuNet 输入张量
  → GPU 执行 YuNet engine，返回原始检测输出
  → CPU 解码出框、分数、五点关键点，按五点对齐人脸
  → GPU 执行 SFace engine，返回每张脸的 128 维特征
  → CPU 归一化、与注册库比较，做跟踪和事件判断
```

## ONNX、TensorRT、engine、trtexec 各是什么

| 名称 | 在这条路线里的职责 | 容易误会的地方 |
| --- | --- | --- |
| `.onnx` | 保存已训练模型的计算图、权重、输入输出定义；YuNet 和 SFace 各有一份 | 文件是通用模型描述，**本身不指定实际用 CPU 还是 GPU** |
| TensorRT | NVIDIA 的模型构建与运行工具；根据模型、输入形状、精度和当前设备选择执行方案 | 它既参与**构建**，也有**运行时接口**；不是模型名称 |
| `.engine` | TensorRT 构建后序列化保存的执行计划，包含该模型在目标环境中的执行选择 | 不等同于通用的 ONNX；项目按板子、TensorRT 版本、输入形状管理它 |
| `trtexec` | TensorRT 自带的**命令行工具**，可读 ONNX、构建/保存 engine，也可加载 engine 跑基准 | 项目用它在准备阶段建 engine；正式服务逐帧推理时调用的是 TensorRT/PyCUDA 接口，**不是每帧启动 `trtexec`** |
| CUDA / PyCUDA | 提供 GPU 执行环境、缓冲区与数据搬运等能力；本项目 Python 封装通过 PyCUDA 调用这些能力 | 装好 CUDA 不代表 `cv2.dnn` 自动改走 GPU |

你说“**TensorRT 把 ONNX 转换成适配 Jetson GPU 架构的格式**”，方向基本正确。更准确地说：**TensorRT 读取 ONNX，在指定输入形状和精度下，为构建时的 GPU 与软件环境选择执行方式，生成可供 TensorRT 运行时加载的 engine。** 它不只是更换文件后缀，也不改变模型的训练任务或身份库。构建时可能选择不同计算核、融合可融合的操作、安排内存；但本项目没有逐层证明具体哪项优化贡献了多少加速。旧版 TensorRT 的 engine 不能假定能随意跨板、跨版本使用，所以项目选择在目标板上构建并校验。可参看 [NVIDIA 的构建示例](https://docs.nvidia.com/deeplearning/tensorrt/latest/getting-started/build-your-first-engine.html)及[本项目 engine 校验记录](部署上板与engine校验.md)。

本项目中，一个直观的构建命令形状是：

```bash
trtexec --onnx=yunet_trt320.onnx --saveEngine=yunet_fp16.engine --fp16
```

其中 `--onnx` 指输入模型，`--saveEngine` 指输出文件，`--fp16` 允许采用 FP16 执行策略。SFace 支持一批多张脸，构建时还要给出 **min=1、opt=4、max=8** 的输入形状范围。`opt=4` 是优化时的参考批量，**不表示运行时只能喂 4 张**。当前项目的构建脚本是 [ensure_engines.py](../scripts/ensure_engines.py)，并非每次都要求手工输入这条命令。NVIDIA 官方也将 `trtexec` 用于[构建和基准测量](https://docs.nvidia.com/deeplearning/tensorrt/10.x.x/performance/benchmarking.html)。

## 实际遇到的难题，按发生顺序看

### 1. 板子有 GPU，但旧程序没有可用的人脸识别路径

**现象：** Jetson 的 OpenCV 4.1.1 缺少 `FaceDetectorYN`、`FaceRecognizerSF` 接口；正式服务退到 Haar。Haar 只给框，没有 YuNet 的五点关键点，SFace 所需的标准人脸对齐无法正常完成。另一方面，板上 OpenCV 没编 CUDA，`cv2.dnn` 执行模型时走 CPU。

**解决：** 先用板上仍有的 `cv2.dnn` 能力搭出“ONNX YuNet → 手工解码五点 → 手工对齐 → ONNX SFace”的完整管线，证明旧系统不升级也能完成识别；然后让同一新管线分别拥有 CPU runner 和 TensorRT runner。这样比较速度时，才有“**同一算法管线，换执行后端**”的对照。[早期板端边界](旧版YuNet隔离链路验收-2026-11-01.md)、[生产接线](P1-ONNX管线接线-2026-11-07.md)

### 2. 找到 ONNX 模型，不等于板端能读、能正确找脸

**现象：** YuNet 2023 模型在板端 OpenCV 的 ONNX 导入阶段失败。旧版 YuNet 2022 也因旧导入器不认识部分 `Slice` 表达方式而失败；SFace 能单独前向，不代表已完成“找脸、对齐、认人”。

**解决：** 对固定的旧版 YuNet 制作**隔离实验副本**：将 3 个常量 `Slice` 改为旧导入器能理解的表示，去掉 5 个值为默认值的 `MaxPool ceil_mode` 属性，标记为 opset 9；用 ONNX checker 和本机同帧逐输出对照，再到板端验证检测、五点、对齐、SFace。原模型保留。这是对**该固定模型**的兼容处理，不是“任意 ONNX 都能降级”。[兼容性实测](模型兼容性实测-2026-11-01.md)、[隔离链路验收](旧版YuNet隔离链路验收-2026-11-01.md)

### 3. ONNX 文件声明的尺寸与实际喂入尺寸不一致

**现象：** 兼容副本的输入声明是 `1×3×120×160`，旧管线实际一直喂 `1×3×320×320`。OpenCV 路径容忍了这种用法；TensorRT 依据声明构建静态 engine 后，运行时喂 320 就会报输入维度越界。

**解决：** 只修正实验副本的**输入尺寸声明**为 320×320，生成 `yunet_trt320.onnx`，通过结构检查与数值对照后再建 engine。直觉上，engine 像按确定规格做好的工装：规格写成 120×160，运行时不能突然塞进 320×320。这里没有重新训练模型。[加速评估](Jetson-TRT加速评估-2026-11-06.md)

### 4. engine 能建出来，还要把数据安全地送进去、取出来

**现象：** Python 不能只把 `cuda.mem_alloc()` 返回的设备内存对象转成整数指针并丢掉原对象。原对象被回收后，指针看起来还在，实际缓冲区已释放，后续拷贝会报 `invalid argument`。SFace 一次处理的人脸数还会变化，输出缓冲也不能永远按一张脸分配。

**解决：** [trt_backend.py](../vision/trt_backend.py)在整个推理期间保留 `DeviceAllocation` 对象；按实际批量检查 profile、设置 binding 形状，复用或扩容输入输出缓冲。每次运行依次做：准备连续张量 → 主机缓冲复制到 CUDA 缓冲 → 执行 engine → 输出复制回来 → 同步后交给解码/识别代码。Jetson CPU/GPU 共享物理内存体系，但这些 API 层面的拷贝和同步仍有成本。[封装与回归](TensorRT封装-2026-11-06.md)

### 5. GPU 前向变快，整段检测却没快到预期

**现象：** 初次封装后检测整段仅约 **83.8 → 45.9 ms**。排查发现，GPU 执行 YuNet 之外，旧 Python 解码器还在**每帧重新生成 5875 个先验框**，约 31 ms；这是与画面内容无关的重复工作。

**解决：** 把先验表改为加载模块时算一次，逐帧只做按分筛选、还原坐标和 NMS。板端同一固定视频 40 帧逐位对照，解码中位时间 **32.689 → 0.799 ms**，脸数和框不变。这里的提速来自 **CPU 少做重复工作**，不是又把解码搬到 GPU。[解码优化](解码先验预计算-2026-11-07.md)

### 6. FP16 更快，但结果必须证明没有把身份算错

**现象：** 用 FP16 构建的 engine 与 CPU FP32 不应预设逐位完全相同。尤其 SFace 的特征向量会用于相似度阈值判断，只有“能运行”不足以验收。

**解决：** 固定同一视频和模型，对照逐帧检出数量、框、特征余弦，以及完整服务的身份与事件。接入时 40 帧、阈值 0.6 的 CPU/TRT 对照为 **79 vs 79 张脸**、框最大差约 **0.0001 px**、特征余弦最低 **0.999978**；完整服务对照中身份与事件条数一致。这说明**这批测试输入**上数值与业务行为接近，不代表所有光照、姿态和新人都已验证。[生产接线验收](P1-ONNX管线接线-2026-11-07.md)

### 7. 多脸批量在旧版 OpenCV CPU 路径产生错误特征

**现象：** 同帧多张脸给 Jetson OpenCV 4.1.1 的 SFace `cv2.dnn` 批量前向时，输出会塌成相同向量，而且内部状态会“粘住”；这不是正常的批量加速效果。PC 上较新 OpenCV 的同样模型未复现。

**解决：** CPU runner 一次只喂 **1 张脸**；TensorRT runner 保留 **最多 8 张**的批量能力。识别器按 runner 上限分块，被限制时记录日志；返回结果还保持与原人脸顺序一一对应，避免把甲的特征配给乙。[问题复现与处置](P1-ONNX管线接线-2026-11-07.md)

### 8. 换环境后 engine 可能失效，自动回退还可能掩盖降速

**现象：** engine 依赖构建环境。换模型、换板或升级 TensorRT 后，旧 engine 不能凭文件存在就算可用；`auto` 模式回退 CPU 后服务仍可运行，容易误以为正在使用 GPU。

**解决：** engine 旁保存 `.engine.json` 校验单，绑定 engine 与源 ONNX 的 SHA256、TensorRT 版本、精度、输入 profile 和输出信息；加载时核对。`ensure_engines.py --check` 做只读体检，需要时重建并复验；部署时再用 `check_backend.py --profile board` 核对检测与识别**实际**都是 TensorRT，心跳也报告后端。`auto` 允许回退并打日志；`trt` 明确要求 engine 可用，否则启动报错。[封装](TensorRT封装-2026-11-06.md)、[部署与校验](部署上板与engine校验.md)

> 重建耗时（Nano 实测）：YuNet 约 **150.6 s**、SFace 约 **483 s**（动态 batch 三档 profile 更慢），
> 看起来"卡住"是正常的；SFace 重建后与生产 engine 在 batch 1/4/8 上逐行余弦 ≥ 0.99955，
> 生产 engine 未被改动（[证据](data/sface-engine-rebuild-2026-11-08.json)）。

### 9. 模型计算快了，摄像头仍可能卡住整条管线

**现象：** 1280×720 USB 摄像头未指定格式时协商到 YUYV，经 USB2 采集仅约 9.95 fps，服务约 6.2 fps；这时再优化 YuNet engine，也无法超过采集瓶颈。

**解决：** 板端明确选 V4L2 + MJPG，记录实际协商与读帧结果；同一阶段生产管线约 **17.6–17.8 fps**。这属于摄像头输入链路修正，不应算成 TensorRT 单独带来的加速。[摄像头板端记录](P2摄像头打通与公开素材复评-2026-11-07.md)

## 最后如何判断这条路线已经接上

项目先在**离线实验**中证明两份 engine 可以跑、输出与 CPU 接近；再完成运行时封装与解码优化；最后接进 `VisionService`。接入阶段同一批测试素材、同一条新 ONNX 管线的整链对照为 **CPU 3.69 fps / 约 271 ms 每帧**，**TensorRT 17.65 fps / 约 56 ms 每帧**，约 **4.8 倍**。这个整链差异包含 GPU 前向、批量策略及其他管线开销，不能写成“FP16 一项带来 4.8 倍”。摄像头实测又是另一套测试条件，也不要和离线 40 帧的数字直接相除。[P1 验收](P1-ONNX管线接线-2026-11-07.md)、[P2 摄像头](P2摄像头打通与公开素材复评-2026-11-07.md)

**理解检查：** 如果现在换了 Jetson 或 JetPack，程序可能仍能在 `auto` 下运行，但应先检查 engine 与模型/环境是否匹配，再看检测和识别各自实际选择的后端，最后用同一素材核对结果和速度。服务“跑着”与“确实在 GPU 上正确地认人”是两项不同的证据。
