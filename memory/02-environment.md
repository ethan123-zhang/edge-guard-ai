---
type: project
topic: 环境与硬件事实
last_updated: 2026-10-15
---

# 环境与硬件事实

> 只记录**事实**与**已确认的约束**。未验证的推测放在 `05-open-questions.md`。

## 1. 硬件清单

### 已确认拥有

| 设备 | 型号 | 已知约束 |
| :--- | :--- | :--- |
| 视觉节点 | **Jetson Nano B01**（4GB） | 128 核 Maxwell，JetPack 最高 **4.6**（Ubuntu 18.04 / CUDA 10.2），**已停更**；系统 Python 为 **3.6** |
| 对话节点 | **鲁班猫5 v1（RK3588）** | 6 TOPS NPU + 8 核 CPU（4×A76 + 4×A55），出厂 Ubuntu。**实测见下方** |
| 摄像头 | 亚博智能 1080p USB | 已验证可用 |
| 音箱 | 最基础款 | 够用 |
| 转换机 | **本机笔记本** | **RTX 4060 Laptop 8GB**，用于 RKLLM 模型转换（详见第 2 节） |

### 鲁班猫5 板端实测（**2026-10-15，实测值取代教程值**）

| 项 | 实测值 | 说明 |
| :--- | :--- | :--- |
| 架构 | `aarch64` | ✅ 符合预期 |
| **NPU 驱动** | **RKNPU driver v0.9.8** | ✅ 与 `docs/04` 原记一致（这次是**本机实测**，不再是抄教程） |
| **`librkllmrt.so`** | **✅ 已自建并跑通** | 出厂时**未预装**；2026-10-15 用 `board-pkg/` 的最小包在板端**本机编译**成功（`build-on-board.sh`），产物在 `~/rkllm-board-v1.3.1/examples/rkllm_api_demo/deploy/install/demo_Linux_aarch64/` |
| **rkllm runtime 版本** | **1.3.1** | ✅ 与 Toolkit 1.3.1、驱动 0.9.8、平台 RK3588 **四者全对齐** |
| 板子 IP | **192.168.3.214** | ⚠️ **与 `docs` 规划的 `192.168.43.10` 不一致**（见下） |
| 登录方式 | **VSCode Remote-SSH** | 用户名 `cat`，已连上（说明 SSH 可用、网络通） |
| 环境 | 有 `(base)` 前缀 | 装了 conda/miniconda |

> ✅ **板端 runtime 已解决，不必再克隆 773M 仓库**：出厂无 `librkllmrt.so`，
> 但**不需要**在板子上 `git clone`（实测只有 37 KiB/s，下不动）。
> 用 PC 打好的最小包（`board-pkg/rkllm-board-v1.3.1.tar.gz`，**2.4MB**）传过去，
> 跑 `./build-on-board.sh` 即可（**不要用官方 `build-linux.sh`**，它写死交叉编译器路径）。
> 步骤与故障表见 `docs/板端验证清单-明天用.md`；坑见 `04` 的 E-12/E-13/E-14。
>
> ⚠️ **IP 与规划不符**：文档规划的是手机热点网段 `192.168.43.x`，
> 但实测板子在 **`192.168.3.214`**（家用/实验室网段）。
> **`config.yaml` 里的地址要以现场实际网络为准**，不要硬编码规划值。
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

**转换用 Linux 环境（V0，2026-10-15 实测可用）**：

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

**现场条件**：教室 + 手机热点 + 电脑演示。

**关键约束**：手机热点后面是 **NAT**，外部连不进来。

```
门口设备(192.168.43.x) → 手机热点 → 运营商 NAT → 互联网
                                              ✗ 外部连不进来
```

**因此**：
- ❌ 不能让主人端从"外网"连设备
- ❌ 不能依赖云端 MQTT（教室热点未必有外网）
- ✅ **主人端连同一个热点，局域网直连**

**已规划的静态 IP**：

| 设备 | IP |
| :--- | :--- |
| 手机热点网关 | 192.168.43.1 |
| 鲁班猫5 | **192.168.43.10** |
| Jetson Nano | 192.168.43.11 |
| 主人端 | DHCP 即可 |

**端口约定**：`8765` = 视觉→对话；`8766` = 对话↔主人端。

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
