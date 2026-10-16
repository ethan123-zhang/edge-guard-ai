---
type: project
topic: 已知坑与风险
last_updated: 2026-10-15
---

# 已知坑与风险

> 记录**还没踩但一定会踩**的坑，以及已经识别的风险。踩到后回来补充实际情况。

## 1. 技术坑（按严重程度）

### 🔴 P0：会导致项目不成立的

| # | 坑 | 现状 | 应对 |
| :--- | :--- | :--- | :--- |
| P0-1 | **RKLLM 模型转换失败/太慢** | 未开始 | 降级 4 级：① 排查 Toolkit/Runtime 版本一致性（**最常见的失败原因**）② 换模型或改 CPU+llama.cpp ③ 固定话术模式 ④ 云端 API（最后手段） |
| P0-2 | **Jetson 人脸识别跑不动** | 未开始 | 降分辨率、跳帧、缩小 ROI；极端情况先写死结果跑通链路 |
| P0-3 | **麦克风噪声导致 ASR 不可用** | 概率**高** | 阵列麦 + AEC + 播报闭麦；**键盘输入模式兜底** |

### 🟠 P1：会导致演示翻车的

| # | 坑 | 说明 | 应对 |
| :--- | :--- | :--- | :--- |
| P1-1 | **自问自答死循环** | TTS 播报被自己录进去，教室混响会放大 | 播报闭麦 + **清空音频缓冲**（最易漏的一步） |
| P1-2 | **观众触发识别** | 教室里走动的人被当成访客 | **ROI 中央区域裁剪**，坐标外置可调 |
| P1-3 | **现场掉线** | 热点抖动、IP 变化、服务没起 | 静态 IP + systemd 自启 + 断线重连 |
| P1-4 | **光照导致识别失败** | 教室顶灯直射 / 窗户逆光 | 补光灯 + **锁定曝光白平衡** + 阈值外置 |
| P1-5 | **Jetson 供电不足随机重启** | Nano 头号"玄学故障" | 5V4A DC 电源 + 短线；**遇到随机重启先换电源** |
| P1-6 | 散热降频 | 两台板子都会，满载掉 30%+ | 散热片 + 风扇（**必须**） |

### 🟡 P2：会影响答辩的

| # | 坑 | 应对 |
| :--- | :--- | :--- |
| P2-1 | **网络延迟数据不可信** | 热点下抖动极大；**只测本地环节延迟**（识别耗时、LLM 首字），不测网络往返 |
| P2-2 | 数据最后补不出来 | **边测边记**到 `docs/data/*.csv` |
| P2-3 | 隐私合规被问 | 准备话术：本地存储、不上云、可删除、门口告知 |

## 2. 流程风险

| # | 风险 | 应对 |
| :--- | :--- | :--- |
| F-1 | **单人开发 + 时间被砍**（主力可能被调去机甲大师） | 功能已砍到最小；**卡 2 小时就换方案** |
| F-2 | 最后一周改环境 | **第 4 周禁止**加功能/改架构/升级系统/装新依赖 |
| F-3 | 改出问题无法回退 | **每周备份能跑的版本** |
| F-4 | 现场真人识别失败 | **演示模式开关**（预置人脸+预置对话），一键切换 |

## 3. 已识别的设计缺陷（诚实记录）

| 缺陷 | 影响 | 状态 |
| :--- | :--- | :--- |
| **无活体检测** | 系统**不能防照片攻击** | 已知最大局限，答辩主动说明 |
| 无多人对话 | 同时来多人只处理置信度最高的一张脸 | 本版本接受 |
| 校准/评估条件未定 | 识别率数字需现场实测 | 待验证 |
| 口罩场景未优化 | 遮挡时识别率下降 | 降级策略已设计，未实现 |

## 4. 环境相关的坑

| # | 坑 | 说明 |
| :--- | :--- | :--- |
| E-7 | **DSH 沙箱降权导致 WSL/Docker/git 全线假性故障（2026-10-15 实测，已解决）** | **根因**：DSH 默认沙箱把进程降到 **Low 完整性级别**（`S-1-16-4096`），且管理员组被标为 *Group used for deny only*。Low IL 下**读不到凭据库、开不了服务**，于是表现为：`wsl -l -v` / `wsl --status` 报 `Wsl/E_ACCESSDENIED`、`LxssManager` 停在 Stopped、Docker 守护进程起不来、`git push` 报 `schannel: SEC_E_NO_CREDENTIALS`。**解法**：用 `danger-full-access` 跑（实测完整性级别 Low→Medium，`S-1-16-8192`），三条链路**同时恢复**。**教训**：`wsl --version` 在 Low IL 下**依然正常**，是最容易误导人的烟雾弹——**别只凭它判断 WSL 好坏**，要跑 `wsl -l -v` |
| E-1 | **RKLLM-Toolkit 只有 Linux wheel** | Windows 装不了，必须用 WSL2 / x86 Linux |
| E-2 | **Jetson Python 3.6** | 别升级；别装 insightface 全家桶 |
| E-3 | **热点后是 NAT** | 外部连不进来，只能局域网直连 |
| E-4 | **本机 GitHub 必须用 HTTPS 远端** | SSH 公钥未加到账号，`git@github.com: Permission denied (publickey)`。详见 `00-maintenance-rule.md` 踩坑记录 |
| E-5 | **沙箱写入限制** | DSH 会话默认 `workspace-write`，**写 `E:\Document\...\aiVault` 会被拒**，需一次性提权；子 agent 提权被拒后**无法重试**，此类跨库写入**应由主会话直接执行** |
| E-6 | `pushToGithub.bat` / `push_ai.bat` 结尾 `pause` | 自动化/非交互环境下会卡住，改用等效的三条 git 命令 |
| E-8 | **WSL 里默认不通宿主机代理（2026-10-15 实测）** | **现象**：Windows 上开着梯子，WSL 里 git/pip 却极慢或直连失败。**根因**：WSL2 是独立虚拟机，里面的 **`127.0.0.1` 指 WSL 自己**，不是 Windows。**解法**：用宿主机在 WSL 网络中的网关地址——本机为 **`172.28.240.1:10090`**（`ip route \| grep default` 可查，会随重启变化）。已写入 `~/.bashrc`、`~/.pip/pip.conf`、`git config --global http.proxy`，脚本见 `scripts/wsl_setup_proxy.sh` |
| E-9 | **沙箱内 Git 自带 bash 跑不了（2026-10-15 实测）** | 跑 `scripts/check_gitignore.sh` 报 `fatal error - couldn't create signal pipe, Win32 error 5`。**根因**：受限沙箱下**无法创建命名管道**。**解法**：改用 PowerShell 做等效检查——`git status --short` + `git ls-files \| Select-String '\.(rkllm\|onnx\|npy\|db\|jpg\|wav)$'`（结果应为空）。**这是预期边界，不要反复重试** |
| E-10 | **git 提交需写 `.git/`，默认沙箱会被拒** | 报 `Unable to create 'E:/哨兵管家/.git/index.lock': Permission denied`。**解法**：提交/推送用 `danger-full-access` 跑（与 E-7 同源） |
| E-11 | **大仓库从 GitHub 克隆易 TLS 中断** | `error: RPC failed; curl 56 GnuTLS recv error (-9)` + `early EOF`。**解法**：走代理 + `http.postBuffer 524288000` + `http.version HTTP/1.1` + **失败重试**（实测第 2 次成功，仓库 773M）。脚本见 `scripts/wsl_clone_rkllm.sh` |
| E-12 | **板子直连 GitHub 只有 37 KiB/s（2026-10-15 实测）** | 在鲁班猫5 上 `git clone rknn-llm`（773M）**基本下不动**。**解法**：**不要在板子上克隆**——在 PC（WSL，走代理）克隆后，**只打包板端编译所需的最小集合**传给板子。实测 **773M → 2.4MB**（压缩后），缩小 300+ 倍。脚本见 `scripts/wsl_make_board_pkg.sh`，产物 `board-pkg/rkllm-board-v1.3.1.tar.gz` |
| E-13 | **官方 `build-linux.sh` 在板子上必然失败** | 它写死了**交叉编译器**路径 `~/opts/gcc-arm-10.2-2020.11-x86_64-aarch64-none-linux-gnu/bin/aarch64-none-linux-gnu-gcc`——那是给 **x86 PC 交叉编译**用的。**板端本机编译要改用系统 gcc/g++**。已提供 `build-on-board.sh` 替代（不指定工具链，走 `cmake` 默认编译器） |
| E-14 | **`deploy` 编译依赖 `rkllm-runtime` 目录** | `CMakeLists.txt` 里写死 `${CMAKE_SOURCE_DIR}/../../../rkllm-runtime/${CMAKE_SYSTEM_NAME}/librkllm_api/`。**只拷 `deploy` 会编译失败**，必须连带 `rkllm-runtime/Linux/librkllm_api/{include,aarch64}`。**好消息**：`librkllmrt.so` 已在仓库内（7.7MB），无需单独下载 |

## 5. 记忆库自身的坑

| # | 坑 | 说明 |
| :--- | :--- | :--- |
| M-1 | **设计先行导致"规划被误写成已完成"** | 本项目文档先于代码，记忆库极易把 `docs/` 里的规划项记成事实 |
| M-2 | ~~项目已建 git 却未建 `.gitignore`~~ | ✅ **已解决**（2026-10-15）：`.gitignore` 已建并验证，模型/数据库/人脸特征/截图均已排除 |

## 相关记忆

- 待验证项 → `05-open-questions.md`
- 部署正文与故障排查手册 → `docs/04-部署与运维.md`（**打印带到现场**）
- 环境事实 → `02-environment.md`
