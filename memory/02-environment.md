---
type: project
topic: 环境与硬件事实
last_updated: 2026-10-17
---

# 环境与硬件事实

> 只记录**事实**与**已确认的约束**。未验证的推测放在 `05-open-questions.md`。

## 1. 硬件清单

### 已确认拥有

| 设备 | 型号 | 已知约束 |
| :--- | :--- | :--- |
| 视觉节点 | **Jetson Nano B01**（4GB，**亚博智能经销**，官方 Dev Kit 规格） | 128 核 Maxwell，JetPack 最高 **4.6**（Ubuntu 18.04 / CUDA 10.2），**已停更**；系统 Python 为 **3.6**。**模块自带 eMMC + 载板有 TF 卡槽 —— 口径见下节** |
| 对话节点 | **鲁班猫5 v1（RK3588）** | 6 TOPS NPU + 8 核 CPU（4×A76 + 4×A55），出厂 Ubuntu。**实测见下方** |
| 摄像头 | 亚博智能 1080p USB | 已验证可用 |
| 音箱 | 最基础款 | 够用 |
| 转换机 | **本机笔记本** | **RTX 4060 Laptop 8GB**，用于 RKLLM 模型转换（详见第 2 节） |

### Jetson Nano B01 板端实测（**2026-10-16 起；2026-10-23 纠正型号口径**）

> ✅ **型号口径（2026-10-23 用户确认 —— 推翻此前所有 "B01 sub" 的写法）**
>
> 这块板**就是 Jetson Nano B01**（模块 `p3448-0002`），**不是亚博的 SUB 版**。
>
> 1. **"既有 eMMC 又有 TF 卡"是 B01 的正常形态**，**不能**当成 SUB 版的标志。
>    板端实测：`mmcblk0` = **14.7 GiB eMMC**（带 `mmcblk0boot0/boot1/rpmb`，一整套 L4T 分区）；`mmcblk1` = **TF 卡**（系统根分区在 `mmcblk1p1`）。
>    （2026-10-22 曾据"有 eMMC"误判成 SUB 版，教训：**型号以用户/官方规格为准，不从分区推断**。）
> 2. **启动链**：eMMC 的 `/boot/extlinux/extlinux.conf` 写着 `APPEND … root=/dev/mmcblk1p1` ——
>    **引导器 + 内核在 eMMC，根分区在 TF 卡**。这是现状，也是这块板的结构。
> 3. **亚博 SUB 版是另一条产品线**（U 盘镜像 + EMMC 引导 + `flash.sh -r jetson-nano-devkit-emmc`），
>    **与本板无关，别照 SUB 的教程操作**。
> 4. 亚博出厂镜像的默认账号是 **`jetson` / `yahboom`**（他们文档原文），与本项目自建的 `jetson` 账号无关。

> 🟢 **2026-10-20 重大进展：官方镜像已确认可用、已下载**
> - 型号口径已纠正为 **Jetson Nano B01**；**官方技术员确认**：这块 B01 板**可以直接用 NVIDIA 官方
>   JetPack 4.6 SD 卡镜像**，不需要亚博定制镜像。
> - 镜像 `jetson-nano-jp46-sd-card-image.zip`（JetPack 4.6 = L4T **R32.6.1**，**6,533,894,369 B / 6.09 GiB**）
>   已下载到 `firmware/`（不入库），SHA256 见 `firmware/*.zip.sha256`，下载脚本 `scripts/download_jetson_jp46.py`。
> - zip 内镜像是 **`sd-blob-b01.img`** —— 与 B01 载板匹配。**A02 镜像的 dtb 在 B01 上起不来**（绿灯亮、HDMI 全黑），
>   别把旧卡插到这块板上"试一下"。
> - **下面这张 2026-10-16 的实测表是"亚博出厂镜像"的画像，烧官方镜像后全部作废，必须按
>   `docs/04` §2.0 末尾的命令重测一遍。** 光烧系统**不等于** V1 就通了：JP46 自带 OpenCV **4.1.1**
>   （有 `dnn`、**无** `FaceDetectorYN`），YuNet/SFace 仍要自编 4.5+；好处是干净系统 + 32G 卡终于**有空间**编。

> ⚠️ **这块板子跑的是亚博智能的「出厂镜像_ROS+AI视觉」**，环境**被预装内容改过**。
> 下面的值**只代表这个亚博镜像，不代表干净的 JetPack**。烧官方镜像后必须重测。

### 🚨 首启与串口：这块板的"命门"（**2026-10-21 两个晚上换来的**）

> **官方镜像没错、卡没坏、板子也没坏** —— 卡住的是"**显示通道**"。

| 事实 | 值 / 做法 |
| :--- | :--- |
| **HDMI 显示通路** | ❌ **不可依赖**：`tegra-i2c no acknowledge from address 0x54`（**读不到显示器 EDID**）+ `dpd enable lookup fail:-19`（显示电源域 ENODEV）。HDMI 上能出启动日志，但图形/向导起不来 |
| **可靠通道** | ✅ **micro-USB 串口**：板端设备名 **`ttyGS0`**，Windows 侧是 `USB Serial Device (COMx)` |
| 串口参数 | **115200 / 8 / 无校验 / 1 停止位 / 流控 None**（PuTTY 默认 XON/XOFF，会被"挂起"成黑屏） |
| 串口工具 | `tools\putty.exe`（便携版，Authenticode 签名 Simon Tatham）；**别用 `plink -serial`**，Windows 控制台里会乱码 + 原样打出 `[10;12H` |
| **首启顺序（铁律）** | **先连串口、窗口打开，再上电**。`nv-oem-config` 在**开机那一刻**就决定走显示还是走串口；它选了串口就一直等，`no limit` 不超时，事后插显示器不会自救 |
| **别做的事** | ❌ 向导进行中断电（用户没建成 → 只能重烧）❌ 用 WSL 离线改卡（`wsl --mount` 不认 USB 可移动介质，`HCS/ERROR_INVALID_DRIVE`）❌ 猜账号（`yahboom` 是亚博镜像的账号，官方镜像没有） |
| 板端账号 | 向导**新建**；登录名不要用 `yahboom` | 

> **当前状态（2026-10-22 晚已闭环，10-23 复核）**：官方 JP46 重烧后首启**向导起不来**（`debconf` 锁），
> 用**离线改卡**（读卡器 → usbipd → WSL2 → 改 ext4）解决：建账号 `jetson`、屏蔽 `nv-oem-config.*`、
> 放开串口 getty、扩分区到 30G。**开机直接到 `login:`，能进系统**。
> 流程见 `docs/04` §2.0「首启刮不起向导 / 报 debconf 锁」；复盘见 `session-logs/2026-10-22b-离线修卡与内核错配.md`。
> ⚠️ 遗留：**内核 4.9.299（eMMC 里的厂商内核）与 TF 卡上的官方模块 4.9.253 错配** → WiFi 等模块类驱动全部不可用。

| 项 | 实测值 | 说明 |
| :--- | :--- | :--- |
| 硬件身份 | **`jetson-nano-devkit`** | `COMPATIBLE_SPEC 3448-300-0002--1--jetson-nano-devkit-`，即**官方 Dev Kit 规格**（NVIDIA 烧入模块，亚博改不了） |
| 规格 | `jetson-nano-qspi-sd` | dtb 的 `compatible` 串，描述 **QSPI 引导 + SD 启动**方式；**不代表没有 eMMC** —— 实测板上有 **14.7 GiB eMMC（`mmcblk0`）**，TF 卡是 `mmcblk1` |
| 系统 | Ubuntu **18.04.6**，内核 `4.9.299-tegra` | 内核构建于 **2022-11-22** → 对应较新的 L4T 32.7.x |
| CPU | **4 核** | |
| CUDA | **10.2**（`/usr/local/cuda-10.2`，`nvcc V10.2.300`） | ✅ 完整可用 |
| Python | **3.6.9**（`/usr/bin/python3` → `python3.6`） | 别升级 |
| **OpenCV** | **3.2.0**，来自 Ubuntu 源 `python3-opencv` | ⚠️ 见下 |
| **`cv2.dnn`** | **❌ 整个模块不存在** | `module 'cv2' has no attribute 'dnn'` |
| **`cv2.cuda`** | **❌ 不存在** | 该 OpenCV 未编 CUDA |
| `FaceDetectorYN`/`SFace` | **❌ 不存在** | 需 OpenCV ≥ 4.5.4 |
| ROS | **Melodic 已装**（`/opt/ros` 仅 **174M**，**未运行**） | 亚博镜像预装；**本项目零使用** |
| 磁盘 | **55G，已用 47G，仅剩 6.6G（88%）** | ⚠️ **不足以编译 OpenCV** |
| 内存 | 3.9G（可用 ~1.4G），Swap 5.9G | 编译时会紧张 |
| 散热 | 起始 **29.5°C** | 正常 |

> 🔴 **这块板子的 Python 视觉推理能力基本为零**（2026-10-16 实测）：
> OpenCV 3.2.0 **没有 `cv2.dnn`、没有 `cv2.cuda`、没有 YuNet/SFace**。
> **三条路全堵**：D2 方案（YuNet）不行、换别的 DNN 模型也不行、GPU 加速也不行。
> **结论：必须在装 OpenCV 4.5+（CUDA 版）之后才能做视觉**，`docs/01` D2 的前提要建立在"先装 OpenCV"之上。

> ⚠️ **两套 OpenCV 并存**：Ubuntu 源 `libopencv-*3.2` + `python3-opencv` 3.2.0（Python 实际用这套），
> 以及亚博自编译的 `libopencv`/`libopencv-dev`/`libopencv-python` **4.1.1**（版本号带 git hash）。
> **将来编译 OpenCV 时，这套 4.1.1 的头文件/库会混进来造成链接冲突**，烧干净镜像可消除。

> ⚠️ **官方 JetPack SD 镜像不预装 OpenCV** —— `docs/04` 原隐含假设"JetPack 自带 CUDA 版 OpenCV"是**错的**。
> 干净系统上要自己装。且 **`pip install opencv-python` 不是可行路径**（详见 `04` §2.2 的警告）。

### 鲁班猫5 板端实测（**2026-10-15，实测值取代教程值**）

| 项 | 实测值 | 说明 |
| :--- | :--- | :--- |
| 架构 | `aarch64` | ✅ 符合预期 |
| **NPU 驱动** | **RKNPU driver v0.9.8** | ✅ 与 `docs/04` 原记一致（这次是**本机实测**，不再是抄教程） |
| **`librkllmrt.so`** | **✅ 已自建并跑通** | 出厂时**未预装**；2026-10-15 用 `board-pkg/` 的最小包在板端**本机编译**成功（`build-on-board.sh`），产物在 `~/rkllm-board-v1.3.1/examples/rkllm_api_demo/deploy/install/demo_Linux_aarch64/` |
| **rkllm runtime 版本** | **1.3.1** | ✅ 与 Toolkit 1.3.1、驱动 0.9.8、平台 RK3588 **四者全对齐** |
| 板子 IP | **192.168.3.214** | ⚠️ **与 `docs` 规划的 `192.168.43.10` 不一致**（详见 `07-network.md`） |
| 登录方式 | **VSCode Remote-SSH** | 用户名 `cat`，已连上（说明 SSH 可用、网络通） |
| 环境 | 有 `(base)` 前缀 | 装了 conda/miniconda |

> ✅ **板端 runtime 已解决，不必再克隆 773M 仓库**：出厂无 `librkllmrt.so`，
> 但**不需要**在板子上 `git clone`（实测只有 37 KiB/s，下不动）。
> 用 PC 打好的最小包传过去，跑 `./build-on-board.sh` 即可
> （**不要用官方 `build-linux.sh`**，它写死交叉编译器路径）。
>
> 📦 **当前应传的包是 v2**：`board-pkg/rkllm-board-v1.3.1-v2.tar.gz`（2.33MB），
> **内含 `llm_demo` 系统提示词补丁**（v1 是官方原版，**人设复验必须用 v2**）。
> 打包脚本 `scripts/make_board_pkg.ps1`（Windows 端，不依赖 WSL）。
>
> ⚠️ **IP 与规划不符**：文档规划的是手机热点网段 `192.168.43.x`，
> 但实测板子在 **`192.168.3.214`**（家用/实验室网段）。
> **`config.yaml` 里的地址要以现场实际网络为准**，不要硬编码规划值。详见 `07-network.md`。
> 现场演示时若改用手机热点，IP 会再变——**静态 IP 配置（`docs/04` §2.5）这一步仍然必要**。

### 必须新购（**阻塞项**）

| 设备 | 为什么必须买 |
| :--- | :--- |
| **USB 阵列麦克风** | 原有麦克风**噪声太大已确认不可用**。教室是混响环境，单麦不可用 |

**麦克风选型要求（三条硬指标）**：
1. 麦克风阵列（≥2 麦，4 麦更好）+ 波束成形
2. **硬件 AEC（回声消除）** —— 否则播报声被自己录进去
3. USB 免驱（Linux `arecord -l` 能识别）

**首选**：ReSpeaker USB Mic Array（4 麦）。
**备选**：带降噪的 USB 会议全向麦。
**不推荐**：ReSpeaker 2-Mic Pi HAT（I2S 接口，鲁班猫 40pin 未必兼容）。

### 建议购买

- 补光灯（USB LED 灯条即可）—— 现场逆光/夜间必需
- 散热片 + 风扇 ×2 —— Jetson 与 RK3588 **均必须**
- Jetson 电源 **5V 4A DC 圆口** —— 供电不足会随机重启（Nano 头号坑）

## 2. 开发环境事实

| 项 | 事实 |
| :--- | :--- |
| 开发机 | Windows（`E:\` 盘） |
| RKLLM-Toolkit | **只有 x86_64 Linux wheel**，Windows 装不了 |
| **RKLLM 最新版本** | **`release-v1.3.1`**（2026-09-29 发布，[仓库](https://github.com/airockchip/rknn-llm)）。⚠️ `docs/04` 与 `docs/06` 原写 **v1.3.0 已过时** |
| **Toolkit wheel 实际文件名** | v1.3.1 提供三个：`rkllm_toolkit-1.3.1-cp310-cp310-linux_x86_64.whl` / `cp311` / `cp312`。⚠️ `docs/04` 写的 `rkllm_toolkit-1.3.0-cp310-...whl` **在 v1.3.1 tag 下不存在** |
| 模型转换要求 | **必须在 x86 Linux 上做**（WSL2 / Docker / 另一台 Linux 机），**Windows 和板子上都装不了** |
| 板端 Runtime | ⚠️ `docs/02` 原记「runtime 1.3.0 / rknpu driver 0.9.8（野火教程环境）」——**这是教程环境，不是本机实测值**。板子到手后**必须以板端实际版本为准**，并让 Toolkit 版本与之**对齐** |
| **本机 C++ 编译器（2026-10-17 实测）** | ✅ **`C:\Program Files\mingw64\bin\g++.exe`，g++ 8.1.0**（支持 C++11）。**用途**：给板端写的 C++ 补丁（如 `llm_demo.cpp`）**可在本机先做编译+链接检查，不必进 WSL**（WSL 常被沙箱挡住，E-7）。做法：写桩文件冒充 `librkllmrt.so`，见 `board-pkg/src/compile-check-stubs.cpp` |

### PC 视觉仿真环境（**2026-10-23 实测**）

> 等 TF 卡期间在笔记本上代跑 Jetson 的视觉节点（`vision/`）。下面这些是**本机事实**，
> 不是板端事实 —— 板端数字必须上板另测（`scripts/vision_selftest.py`）。

| 项 | 实测值 | 影响 |
| :--- | :--- | :--- |
| Python / OpenCV | **3.10.1** / **opencv-python 4.13.0.92** | `dnn` ✅、`FaceDetectorYN` ✅、`FaceRecognizerSF` ✅ |
| 其他库 | PyYAML 6.0.3、numpy 2.2.6、websocket-client 1.9.2 | WS 客户端**不用**它（板端装了也白装），自己实现 |
| 摄像头 | `idx0` 640×480 **黑场**；`idx1` 1280×720 显示 **intel 标志**（虚拟摄像头） | **拿不到真人画面**；仿真用图片目录回放（`--source`） |
| 视觉模型 | **YuNet / SFace 都没有** | 只能跑 Haar + 无识别降级档，见 E-51 |
| 出网 | **github / pypi / huggingface / jsdelivr 全部 403**（沙箱策略代理） | 模型只能由用户浏览器下载 |
| WSL / Docker | `wsl -l -v` 仍被沙箱挡（`Wsl/E_ACCESSDENIED`）；Docker 守护进程未启动 | 本轮**没做** arm64 保真环境（用户选的是 A 方案） |
| 中文路径 | `cv2.imread/imwrite` 在 `E:\哨兵管家\...` 下**静默失败**（返回 None/False） | 已封装 `vision/imageio.py`，见 E-48 |



| 项 | 实测值 |
| :--- | :--- |
| 方案 | **WSL2**（不是 Docker——Docker Desktop 守护进程起不来，且其发行版本身就是 WSL2 后端） |
| 发行版 | `Ubuntu-22.04`，**Running**；另有 `docker-desktop`（Stopped） |
| 架构 / Python | **x86_64** / **Python 3.10.12**、pip 22.0.2 → 对应 **`cp310`** wheel |
| 资源 | `/` 可用 **943G**；内存 **7.6G**（可用 7.1G）；WSL 用户 `ethan` |
| 网络 | GitHub ✅ 200、ModelScope ✅ 302 |
| **调用前提** | ⚠️ **必须 `danger-full-access`**：默认沙箱把进程降到 Low IL，`wsl -l -v` 会被拒（见 `04` E-7） |
| 工作目录 | `~/rkllm-work/rknn-llm`、`~/sentinel-models`（WSL 内，非 `/mnt/e`） |
| 未装 | conda、git-lfs（**都不需要**，直接用系统 python3.10 + pip） |

**ACL 回滚命令存档（2026-10-15）**：

> `E:\哨兵管家` 目录曾在 2026-10-15 因**缺 WRITE_OWNER 权限**导致连列目录都失败，
> 用 `diagnose-windows-sandbox-acl` 技能修复（详见 `04` 的 E-7a）。
> 当时产生的备份目录 `E:\哨兵管家-acl-recovery\`（原始 DACL 快照 + 回滚脚本 + 审计流水）
> **已于 2026-10-15 清理删除** —— 修复后验证为 `verified` 且长期正常，无回滚价值。
>
> **回滚命令模板**（万一将来需要退回某个 DACL，按此调用，**需自行重新生成快照**）：
>
> ```powershell
> # 修复（会在 -Out 目录写出快照 + 独立回滚脚本）
> pwsh -NoProfile -File diagnose-windows-sandbox-acl.ps1 `
>   -Path 'E:\哨兵管家' -AllowRoot 'E:\哨兵管家' -Out '<恢复记录目录>'
>
> # 回滚（-Restore 只针对单个 -Path，需 WRITE_DAC；保留 owner 与 SACL）
> pwsh -NoProfile -File '<快照>.json.ps1' `
>   -Path 'E:\哨兵管家' -AllowRoot 'E:\哨兵管家' -Restore '<快照>.json'
> ```
>
> ⚠️ **不要依赖已删除的旧快照**：它记录的是**修复前**的空 DACL（`D:`），
> 退回它等于**把目录权限清空**，会让问题重现。要回滚请用**当次修复**产生的快照。

**🔥 转换机器的 GPU（2026-10-15 实测，重要）**：

| 项 | 值 |
| :--- | :--- |
| GPU | **NVIDIA GeForce RTX 4060 Laptop，8GB 显存** |
| 驱动 / CUDA | NVIDIA-SMI 615.65.06，CUDA UMD 13.4；torch 2.6.0+cu124 |
| `torch.cuda.is_available()` | **True** |

> ⚠️ **这推翻了 `docs/04` 的原假设**：原文档写「用 `device='cpu'`」，并配了 `docs/06` 的
> CPU 路线（llama.cpp）作降级。**实测本机有可用 N 卡，转换应走 `device='cuda'`，快得多**。
> 注意官方 `export_rkllm.py` 开头有 `os.environ['CUDA_VISIBLE_DEVICES']='0'`，本机单卡可保留。

> **这条是本项目最重要的环境约束**：模型转换必须提前在 WSL2 里启动，
> 且要下载数 GB 模型 + 量化，**耗时很长**。

## 3. 网络方案事实

> 📄 **正文已拆到 `07-network.md`**（本文件曾超 200 行硬上限）。
> 那里有：现场拓扑与 NAT 约束、**IP 规划值 vs 实测值的对照（鲁班猫5 实测 `192.168.3.214`）**、端口约定。

## 4. 已验证的环境（**2026-10-15 起有记录**）

> 验证结果只写**结论**；原始数字在 `docs/data/latency.csv`，分析见 `docs/data/README.md`。

- [x] **RK3588：`rkllm init success` 跑通** ✅（2026-10-15）
- [x] **RK3588：实测 tokens/s** ✅ —— Generate **31.65 tok/s**、Prefill **198.46 tok/s**、
      首字 ≈**117ms**、峰值内存 **977.87MB**（**答辩核心数字**）
- [x] **RK3588：runtime 版本对齐** ✅ —— Toolkit 1.3.1 ↔ Runtime 1.3.1 ↔ 驱动 0.9.8 ↔ RK3588
- [ ] Jetson：OpenCV 自带版本是否含 `FaceDetectorYN` / `FaceRecognizerSF`（**= V3，下一步**）
- [ ] Jetson：摄像头单帧检测+识别耗时（**基线数字，答辩要用**）
- [ ] 麦克风：`arecord -l` 是否能识别新买的阵列麦（**未购，阻塞项**）
- [ ] 两台设备：手机热点下能否互通
- [ ] RK3588：连续对话 5 分钟后的 tok/s（验散热，风扇已加）

> ⚠️ **仍未验证`端到端`延迟**：ASR/TTS 尚未接入，31.65 tok/s 只是**纯 LLM 环节**的数字。
> 不要把它当成端到端指标对外说。

## 相关记忆

- 设计决策 → `03-design-decisions.md`
- 环境相关的坑 → `04-pitfalls-and-risks.md`
- 部署步骤正文 → `docs/04-部署与运维.md`
