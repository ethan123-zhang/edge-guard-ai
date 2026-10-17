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
| E-7a | **`E:\哨兵管家` 目录级 ACL 缺失导致连列目录都失败（2026-10-15 实测，已修复并清理）** | **现象**：`Get-ChildItem E:\哨兵管家` 失败，报 `SetNamedSecurityInfoW failed (Win32 5): grantWrite`。**根因**：当前登录账号在仓库目录上**缺 WRITE_OWNER 权限**，DSH 无法设置工作区授权（**注意这是 E-7 之外的另一个坑**：E-7 是进程被降权，本项是目录本身缺权限）。**解法**：跑 `diagnose-windows-sandbox-acl` 技能，给该目录补完全控制。**实测**：`writeOwner` false → true，列目录恢复正常；修复动作仅 `backup → grant_dacl → verification(verified)` 一步，**只动了 `E:\哨兵管家` 一个对象**。**备份已清理**（2026-10-15 的 `E:\哨兵管家-acl-recovery\` 已删除）——回滚命令模板见 `02-environment.md` §2 的"ACL 回滚命令存档"。**若要重做**：直接重跑该技能，**不要依赖旧快照**（旧快照记的是"修复前"的 `D:` 空 DACL，已无回滚价值） |
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
| E-15 | **官方 demo 里 `rkllm_set_chat_template` 的注释是错的（2026-10-17 实测）** | `llm_demo.cpp` 把该调用注释成了 **4 个**字符串参数（`""`, `"<｜User｜>"`, `"<｜Assistant｜>"`, `"<｜endoftext｜>"`），但 `rkllm.h:463` 的 1.3.1 签名只有 **3 个**（`system_prompt`, `prompt_prefix`, `prompt_postfix`）。**照抄注释直接取消注释会编译不过**。**正解**：传 3 个参数，`prefix`/`postfix` 给空串，角色标记交给模型内置 chat template。**这也解释了"裸跑没人设"**——不是缺 API，是这行被注释了 |
| E-16 | **Qwen3 的 `enable_thinking` 会偷吃 `max_new_tokens` 预算（2026-10-17 识别）** | `rkllm.h:188` 有 `bool enable_thinking`（Qwen3 专用）。若开启，模型**先吐推理过程**，`max_new_tokens=48` 会被推理吃掉，最终回复看起来"被截断"。**危险在于会误判**：把"thinking 占预算"错判成"48 不够"或"人设失败"。**解法**：显式 `rkllm_input.enable_thinking = false` 并**打印该状态**，把混淆变量钉死。补丁已含此项 |
| E-17 | **Windows 上编辑的文本文件带 UTF-8 BOM，会污染提示词开头（2026-10-17）** | 提示词文件若含 BOM（`EF BB BF`），模型会先读到 3 个乱码字节，可能直接带偏人设。**解法**：读取时检测并剥掉 BOM（补丁已实现，实测 37→34 字节）。**推广**：任何喂给模型的文本文件都应做此处理 |
| E-18 | **本机有 g++，补丁验证不必进 WSL（2026-10-17 发现）** | `C:\Program Files\mingw64\bin\g++.exe`（g++ 8.1.0，C++11 够用）。**意义**：WSL 常被沙箱挡住（E-7），而**写桩文件冒充 `librkllmrt.so` 即可在本机完成编译+链接检查**，上板前先排掉语法/类型错误。桩见 `board-pkg/src/compile-check-stubs.cpp`。⚠️ 沙箱禁止子进程管道，**别用重定向 stdin 喂交互程序**（会挂住）——改用「宏改名 main + 自写 driver」绕过交互循环 |
| E-19 | **Windows 打的 tar 包丢失 Unix 执行位，板端 `./x.sh` 报「权限不够」（2026-10-17 实测）** | **现象**：板端 `./build-on-board.sh` → `bash: ./build-on-board.sh: 权限不够`。**根因**：包是 Windows 上用 **bsdtar** 打的，而 **Windows tar 无法写入 Unix 权限位**（实测 `--mode` 直接报 not supported），包内 `.sh` 全是 `-rw-rw-rw-`。**对照**：v1 包在 **WSL(Linux) 里打的**，保留 `-rwxr-xr-x`，**所以 v1 没这个问题** —— 这解释了"为什么以前没遇到"。**解法（三选一）**：① 板端 `bash fix-perms.sh`（**包内已附**）② 板端 `chmod +x *.sh` ③ 直接用 `bash xxx.sh` 调用，不依赖执行位。**检查方法**：`tar -tvzf 包名 \| grep '\.sh'`，看是 `rwx` 还是 `rw-`。**通用教训**：**跨平台打包时，文件权限/owner 是容易丢的元数据，打完包要 `tar -tvzf` 验一遍** |
| E-20 | **`Cannot open shared object file` ≠ 二进制有问题（2026-10-17 实测，曾误判）** | **现象**：跑 `./llm_demo` 报 `error while loading shared libraries: librkllmrt.so: cannot open shared object file`。**根因**：**只是没设 `LD_LIBRARY_PATH`**，二进制本身好的。**⚠️ 我犯的错**：把"跑 `./llm_demo` 看 usage"当补丁自检，**没设库路径** → 加载失败 → 空输出 → **误报"补丁没进去"**。**⚠️ 同一个错犯了两遍**：先写在 `build-on-board.sh`，修完**又发现 `verify-persona.sh` 里有同样一份**（且板子上跑的正是那一份）。**正解**：判据要用**不依赖动态库**的方式 —— `strings <binary> \| grep -c system_prompt_file`（≥1 = 补丁在）。**通用教训**：① **别把"运行失败"和"内容不对"混成一个判据**；② **修了一个 bug 要全库搜同类**（`grep` 一遍所有脚本里的同样写法），否则只是打地鼠。已全库审计：所有判定改为 strings/grep，仅两处真正执行二进制（均带 `LD_LIBRARY_PATH`） |
| E-21 | **`./llm_demo: 没有那个文件或目录` 有两个完全不同的原因（2026-10-17 实测，用户当成"文件不存在"）** | **现象**：`./llm_demo ... on` → `bash: ./llm_demo: 没有那个文件或目录`，但文件明明在。**根因**：Linux 下这句报错**既表示"文件不存在"，也表示"它依赖的动态库找不到"**（`librkllmrt.so` 未加载）。这里属于后者 —— **报的是库，不是 `llm_demo`**。**触发条件**：单独手敲命令时漏了 `export LD_LIBRARY_PATH=./lib:$LD_LIBRARY_PATH`（脚本里本来有，但用户只复制了命令那一行）。**区分方法**：`ls -la ./llm_demo`（在）+ `ldd ./llm_demo \| grep rkllm`（`not found` = 依赖缺失）。**解法**：① 前缀式 `LD_LIBRARY_PATH=./lib ./llm_demo ...`（一行搞定，不会漏）② 脚本 `--run` 模式（脚本自己 export）。**通用教训**：**给人类的多行命令块极易被只复制最后一行** —— 要么给单行可直接粘贴的形式，要么让脚本自己 `export`，**不要依赖用户执行前置行** |

## 5. 记忆库自身的坑

| # | 坑 | 说明 |
| :--- | :--- | :--- |
| M-1 | **设计先行导致"规划被误写成已完成"** | 本项目文档先于代码，记忆库极易把 `docs/` 里的规划项记成事实 |
| M-2 | ~~项目已建 git 却未建 `.gitignore`~~ | ✅ **已解决**（2026-10-15）：`.gitignore` 已建并验证，模型/数据库/人脸特征/截图均已排除 |

## 相关记忆

- 待验证项 → `05-open-questions.md`
- 部署正文与故障排查手册 → `docs/04-部署与运维.md`（**打印带到现场**）
- 环境事实 → `02-environment.md`
