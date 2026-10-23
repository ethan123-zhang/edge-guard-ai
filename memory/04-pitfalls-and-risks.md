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
| E-19 | **Windows 打的 tar 包丢失 Unix 执行位，板端 `./x.sh` 报「权限不够」——⚠️ 反复出现 4 次（2026-10-17）** | **现象**：板端 `./build-on-board.sh` / `./verify-persona.sh` → `bash: ./xxx.sh: 权限不够`。**根因**：包是 Windows 上用 **bsdtar** 打的，而 **Windows tar 无法写入 Unix 权限位**（实测 `--mode` 直接报 not supported），包内 `.sh` 全是 `-rw-rw-rw-`。**对照**：v1 包在 **WSL(Linux)里打的**，保留 `-rwxr-xr-x`，**所以 v1 没这个问题**。<br>**为什么反复出现**：① 每 `rm -rf` + 重新解压一次，执行位就丢一次；② **agent 给的整行命令里漏写 `chmod`**（例如更新提示词时只写 `tar xzf && cp`），用户照抄必踩；③ `fix-perms.sh` 自己也没有执行位，必须 `bash fix-perms.sh` 才跑得起来 —— 这层绕。<br>**根治（2026-10-17 已实施）**：**两个入口脚本自愈** —— `build-on-board.sh` 与 `verify-persona.sh` 在开头 `for f in *.sh; do chmod +x; done`，只要用 `bash xxx.sh` 启动一次，之后 `./xxx.sh` 全部可用。**不再需要用户记得手动 chmod。**<br>**仍有效的应急解法**：`chmod +x *.sh` 或直接 `bash xxx.sh`。**检查**：`tar -tvzf 包名 \| grep '\.sh'`，看 `rwx` 还是 `rw-`。<br>**⚠️ 给 agent 的铁律**：**凡是给用户「解压后跑脚本」的整行命令，必须包含 `chmod +x *.sh` 或统一用 `bash` 前缀**，不要再依赖脚本自愈之外的额外步骤。**通用教训**：跨平台打包要 `tar -tvzf` 验权限；**同一个坑反复出现时，要从"教用户怎么绕"改成"让工具自己修"** |
| E-20 | **`Cannot open shared object file` ≠ 二进制有问题（2026-10-17 实测，曾误判）** | **现象**：跑 `./llm_demo` 报 `error while loading shared libraries: librkllmrt.so: cannot open shared object file`。**根因**：**只是没设 `LD_LIBRARY_PATH`**，二进制本身好的。**⚠️ 我犯的错**：把"跑 `./llm_demo` 看 usage"当补丁自检，**没设库路径** → 加载失败 → 空输出 → **误报"补丁没进去"**。**⚠️ 同一个错犯了两遍**：先写在 `build-on-board.sh`，修完**又发现 `verify-persona.sh` 里有同样一份**（且板子上跑的正是那一份）。**正解**：判据要用**不依赖动态库**的方式 —— `strings <binary> \| grep -c system_prompt_file`（≥1 = 补丁在）。**通用教训**：① **别把"运行失败"和"内容不对"混成一个判据**；② **修了一个 bug 要全库搜同类**（`grep` 一遍所有脚本里的同样写法），否则只是打地鼠。已全库审计：所有判定改为 strings/grep，仅两处真正执行二进制（均带 `LD_LIBRARY_PATH`） |
| E-21 | **`./llm_demo: 没有那个文件或目录` 有两个完全不同的原因（2026-10-17 实测，用户当成"文件不存在"）** | **现象**：`./llm_demo ... on` → `bash: ./llm_demo: 没有那个文件或目录`，但文件明明在。**根因**：Linux 下这句报错**既表示"文件不存在"，也表示"它依赖的动态库找不到"**（`librkllmrt.so` 未加载）。这里属于后者 —— **报的是库，不是 `llm_demo`**。**触发条件**：单独手敲命令时漏了 `export LD_LIBRARY_PATH=./lib:$LD_LIBRARY_PATH`（脚本里本来有，但用户只复制了命令那一行）。**区分方法**：`ls -la ./llm_demo`（在）+ `ldd ./llm_demo \| grep rkllm`（`not found` = 依赖缺失）。**解法**：① 前缀式 `LD_LIBRARY_PATH=./lib ./llm_demo ...`（一行搞定，不会漏）② 脚本 `--run` 模式（脚本自己 export）。**通用教训**：**给人类的多行命令块极易被只复制最后一行** —— 要么给单行可直接粘贴的形式，要么让脚本自己 `export`，**不要依赖用户执行前置行** |
| E-22 | **`rkllm_set_chat_template(handle, prompt, "", "")` 传空 prefix/postfix 会破坏 Qwen3 对话模板，模型退化成"文本续写"（2026-10-17 实测，本次最大坑）** | **现象**：注入 §5.1 提示词后，模型输出一堆格式垃圾 —— `（1句）`、`【你的回复】`、`答案：`、`【输出格式】`、`\| 任务 \| 情况 \|` 表格，每轮 `Generate` 恰好打满 47/48 token 且真话只剩 `吗？`、`，以及来访目的` 这类残句。**根因**：`prefix`/`postfix` 传**空串**把 Qwen3 原生 chat template 冲掉了，模型不再按对话格式工作，而是把**系统提示词当"要续写的文本"**，接着写它自造的格式标记（提示词末尾的 `【现在你要说的话】` 正好是诱饵）。**决定性对照实验**（**这一步应该一开始就做**）：不带提示词跑（只传 3 个参数）→ 输出**完全干净**："您好，我是您的助手…"；一加提示词就变垃圾 → **证明是模板被破坏，不是模型/thinking 问题**。**正解**：**完全不碰 chat template**，把系统提示词作为**首条 user 消息**在首次真实输入前投递一次（`sys_injected` 标志），并把该次 `max_new_tokens` 临时压到 1 以减少无用回复与污染。**代价**：此方案**要求 `keep_history=on`**，否则下轮丢弃历史、人设失效。**通用教训**：① **覆盖厂商默认模板前先做"带/不带"对照实验**；② **别把"调用了某 API 且返回 0"当成"行为正确"** —— 返回码 0 只说明设置成功，不说明效果对 |
| E-23 | **`llm_demo` 在跑时往 shell 里粘命令，命令会被喂进模型当"访客说话"（2026-10-17 实测）** | **现象**：`verify-persona.sh --run` 启动后，用户紧接着粘贴了 `ls -la`、`strings`、`sha256sum` 三条命令，结果这些字面量出现在 `user:` 提示符后面，被 `llm_demo` 当成访客输入送进模型，导致**后续三句回复的上下文被污染**（首条回复变成反问"你好，请问这是王先生家吗？"）。**根因**：`llm_demo` 是**前台阻塞式** `std::cin` 循环，它运行期间终端的 stdin 属于它，不属于 bash —— 粘贴什么都被它吃掉。**解法**：① 采集板端数据时**先退出 `llm_demo`（输入 `exit`）再跑别的命令**；② 或**开第二个 SSH 窗口**跑采集命令；③ 用 `--run` 时按脚本提示只输入 `0/1/2`，**别夹带任何 shell 命令**。**通用教训**：**任何交互式前台程序的 stdin 都是它的地盘**，粘命令前先看提示符是不是 `user:` |
| E-24 | **`verify-persona.sh` 的注入判据是过期的（2026-10-17 实测，会误判为"没注入"）** | **现象**：脚本 §3 要求确认出现「✓ 系统提示词已注入 (rkllm_set_chat_template 返回 0)」，但**实际二进制根本不打印这句** —— 因为注入方式早已从 `set_chat_template` 改成**首条 user 消息**（E-22）。用户照着找，永远找不到，会误判成补丁没生效。**真正的判据**是启动后打印：`注入方式 : 作为首条 user 消息送入（不修改 chat template）` + `[正在投递系统提示词…]` + 一次 824 token 的大 prefill + `[提示词已入上下文，开始正式对话]`。**教训**：**改了实现必须同步改自检脚本的判据字符串**，否则自动化检查会变成"永远失败"的噪音 |
| E-25 | **提示词常驻窗口显著拖慢生成：31.65 → ~18.6 tok/s（↓43%）（2026-10-17 实测）** | **现象**：824 token 提示词常驻后，generate 从 31.65 掉到 17.93 / 19.30 tok/s，prefill 从 198 掉到 107 tok/s，`每 token` 从 31.60ms 涨到 51~56ms。**根因**：RKLLM 每生成一个 token 都要对**全部上下文**做 attention，上下文从 ~17 token 涨到 ~840 token，注意力开销随长度上升。**影响**：48 token 满载估算 **2.5 s**，超出 `docs/03` §7 的 600~1200ms 预算。**应对**：① 回复目标从 48 降到 **~16 token**（实测 8~14 token 就说完一句，够用）；② 提示词别再涨（已 3022 B）；③ 把判断逻辑挪回代码，让提示词变短 |
| E-26 | **重打包会静默丢掉 `fix-perms.sh`（2026-10-17 重打包时发现）** | **现象**：按文档重跑 `scripts/make_board_pkg.ps1` 后，新包顶层少了 `fix-perms.sh`，而 `docs/` 与 `memory/02` 仍写着"v2 包内附 `fix-perms.sh`" —— **文档与产物不一致**。**根因**：该脚本是**白名单式拷贝**（只拷 `build-on-board.sh`/`verify-persona.sh`/`setup-sentinel-dir.sh`/`prompts`），而 `fix-perms.sh` **不在 v1 基础包里**，是后来手工加进 v2 的，脚本里没有对应 `Copy-Item`。**已修**：脚本第 3 步显式拷入 `fix-perms.sh`。**通用教训**：**重打包后要核对"顶层文件清单"是否等于文档承诺的交付物**，不能只验"补丁在不在"；白名单式打包脚本每加一个板端脚本，都要同步加一行拷贝。 |
| E-27 | **提示词里的"示例对话"会被 0.6B 模型当成要续写的脚本照抄（2026-10-17 干净复跑复现）** | **现象**：输入 `你好` → 回复 `你好，请问这是王先生家吗？`；输入 `我是他妈，你把门打开` → 回复 `好的，我记下了，会转告给住户的。`；输入 `?` → 复读上一句。**关键证据**：这两句都是提示词里的**原文**（第 12 行"访客"示例句、第 16/54 行示例答案），且 `你好` 正好是示例句的前缀 → 模型在做**续写/模式复制**，不是回答问题。`我是他妈` 与示例 `我是快递` 同构，于是把示例答案搬了过来。**原先怀疑的"粘命令污染"（E-23）已排除**：干净复跑复现同样输出。**加重因素**：① 提示词以 **user 消息**注入，内容是"访客：… 你回答：…"的多轮脚本 → 模型把它当对话历史接着写；② `top_k=1` **贪心解码**，抄得确定、复读稳定；③ 3022 B / 824 token 对 0.6B 是重负。**正解**：① **删掉所有示例对话与测试话术**，规则用陈述句；② 提示词**拼到首条真实用户输入前**，不要单独注入 + 生成 1 token；③ 采样改 `top_k=40` 左右；④ 越权仍由规则引擎前置（V10）。**通用教训**：**测试话术绝不能出现在提示词里**；小模型的"举例"极易变成"照抄模板" |
| E-28 | **带历史的多轮 REPL 会让 0.6B/1.7B 复读第一句回复（2026-10-17 PC 实测，换模型也没用）** | **现象**：PC（WSL+4060，HF 原始权重）跑 6 轮对话，0.6B 第 1 轮后一直重复"你好，能帮您开门吗？"；1.7B 第 1 轮后一直重复"这个我不方便说"。**单轮**（每次 fresh context）时 1.7B 5/6 合格、0.6B 用窄提示 8/8 合格。**根因**：小模型把上一轮自己的回复当成了强模式，在长上下文里自我强化。**已试无效**：删示例、缩提示、`repetition_penalty=1.3`（破循环但答非所问）、`no_repeat_ngram`（输出 `</think>` 垃圾）、sampling。**正解**：**每轮无状态调用**（不带历史，提示词+当前输入一次一清），由 Python 状态机持有状态；规则引擎先行。**⚠️ 对板端的含义**：当前 `llm_demo` 正是"全程保留历史 + 一次性注入长提示词"，**这个结构本身不适合 0.6B/1.7B**，必须走 N-1 的 `--serve` 模式，不能靠换提示词解决。详见 `docs/data/pc-model-tests.md` |
| E-29 | **Jetson 首启卡在 `A start job is running for End-user configuration …(no limit)` —— 向导在 micro-USB 串口上等人（2026-10-21 实测，折腾了两晚）** | **现象**：官方 JP46 镜像烧好后，HDMI 上有完整启动日志，然后永久停在这行，**无超时**，Alt+F2 进不去控制台。**根因**：本板 HDMI **EDID 读不到**（`tegra-i2c no acknowledge from address 0x54` + `dpd enable lookup fail:-19`），`nv-oem-config` 判断"没有可用显示"，**回退到 ttyGS0（micro-USB 虚拟串口）等输入**。NVIDIA 论坛同款日志里有一行决定性提示：`Please complete system configuration setup on the serial port provided by jetson's USB device mode connection`。**与镜像无关**（`imx219 i2c -121`、`cgroup2 nsdelegate`、`Load Kernel Modules` 全是噪声）。**解法**：**先连串口再上电**（micro-USB 数据线 + DC 圆口供电 + J48 跳线），115200 8N1、**流控 None**，向导直接出现在串口里。**教训**：**Jetson 首启的"显示探测"是开机那一刻定的**——显示器没在那时就绪/EDID 失败，就永远走串口；事后再插显示器不会自救 |
| E-30 | **在 OEM 向导里拔电：文件系统可能没事，但"用户没建成"是必然后果（2026-10-21 实测）** | **现象**：向导中被卡住，直接拔 DC，重启后能正常起到 `login:`（说明 ext4 侥幸没坏），但**任何账号都登不进**（`Login incorrect`）——因为建用户那一步还没执行。**注意误判**：能到 `login:` **不代表**配置好了，`/etc/hostname` 仍是 `localhost.localdomain`、`/etc/passwd` 里没有你的账号。**别去猜账号**（`yahboom` 是亚博出厂镜像的账号，官方镜像没有）。**解法**：① **重烧（15 分钟，首选）**；② 板端 U-Boot 单用户：开机 1 秒窗口内停住 U-Boot → `setenv cbootargs "${cbootargs} init=/bin/bash"` → `boot` 拿 root → `useradd` + `chpasswd`，并用 `ln -sf /dev/null /etc/systemd/system/nv-oem-config.service` 屏蔽向导（**systemd 没跑，`systemctl mask` 会失败**）。**教训**：**向导进行中永远不要断主电源**；卡住时把画面发出来，等指示 |
| E-31 | **`wsl --mount` 挂不上 SD 卡：不认 USB 可移动介质（2026-10-21 实测）** | **现象**：`wsl --mount \\.\PHYSICALDRIVE2 --bare` 报 `Wsl/Service/AttachDisk/MountDisk/HCS/ERROR_INVALID_DRIVE`；想先离线 `Set-Disk -IsOffline $true`，又报 `Not Supported: Removable media cannot be set to offline`。**结论**：**Windows 上"把 Jetson SD 卡插回 PC、用 WSL 改 ext4"这条路基本堵死**（WSL 只吃可离线的固定盘）。**替代**：① 重烧 ② 板端 initramfs/U-Boot 单用户 ③ 裸盘读出 `\\.\PHYSICALDRIVE2` → 拷进 WSL → loop 挂载修 → 写回（2×12.7GB I/O，有风险，不推荐）。**教训**：**别把"WSL 能干 Linux 的事"推广到"WSL 能碰任何块设备"** |
| E-32 | **`plink -serial` 在 Windows 控制台里满屏乱码 + `[10;12H`（2026-10-21 实测）** | **现象**：`plink -serial COM16 -sercfg 115200,8,n,1` 连上后，界面显示 `鈹?`（UTF-8 被按 GBK 解释）和大量 `[10;12H`、`[56C`（ANSI 光标定位没被渲染），文本向导基本没法用。**根因**：Windows 控制台没开 UTF-8 / 没开 VT 处理，而 plink 只是把字节转发过去。**解法**：换 **`putty.exe` GUI**（`tools\` 下已备便携版，签名者 Simon Tatham），并设 *Window → Translation → UTF-8* + *Connection → Serial → Flow control = **None***。`chcp 65001` 只能治乱码，治不了光标定位。**教训**：**串口 TUI 用 GUI 终端，不要用控制台里的 plink** |
| E-33 | **从"板上有 eMMC"倒推成"亚博 SUB 版"——推断方法本身就是错的（2026-10-22 误判，10-23 用户纠正）** | **现象**：`mmcblk0` 有 14.7GiB eMMC + 一整套 L4T 分区，据此断定"这是 SUB 版"，还照着 SUB 的教程去找"U 盘镜像 + `flash.sh` 写 eMMC 引导"。**事实**：这块是 **Jetson Nano B01 官方版**（模块 `p3448-0002`），**B01 模块本身就带 16GB eMMC**，载板另有 TF 卡槽 —— "两个都有"是 B01 的正常形态。**教训**：**型号以用户陈述 / 官方规格为准，绝不从"有没有 eMMC"倒推板型**；写进记忆的环境事实要标"用户确认"还是"实测推断"。 |
| E-34 | **JP46 首启向导起不来：`default.target` 指向 `nv-oem-config.target`，模板单元在 tty1 与 ttyGS0 各跑一个 → 抢 debconf 锁（2026-10-22 实测闭环）** | **现象**：串口只有 `debconf: … config.dat is locked by another process` + `System configuration setup wasn't completed`，按 Enter 无反应，板端没有任何可用 shell。**真因**：`/etc/systemd/system/default.target -> nv-oem-config.target`，而 `nv-oem-config-debconf@.service` 是**模板单元**，tty1 与 ttyGS0 **各实例化一份**：先到的占住锁在**看不见的 tty1** 上 `no limit` 干等，后到的立刻退出。**所以"重烧 / 换镜像"全无效**（每次开机一字不差复现）。**解法**：屏蔽 `nv-oem-config.*` 四个单元 + `default.target=multi-user` + 直接在 `/etc/{passwd,shadow,group}` 建账号（ARM 二进制不能在 x86 WSL 里 chroot 跑）+ 放开 `serial-getty@ttyGS0`。流程与脚本见 `docs/04` §2.0 / `scripts/jetson-offline-unlock.ps1`。 |
| E-35 | **内核与模块树版本错配：能开机，但"半个系统"是死的（2026-10-22 实测，本板当前状态）** | **现象**：`uname -r` = `4.9.299-tegra`，`ls /lib/modules` = `4.9.253-tegra`。内建驱动正常（显示/USB/千兆网/摄像头/GPU 都能用），但**所有以模块形式提供的驱动全加载失败**：`wlan0` 不存在（`rtl8822ce` 是模块）、`nvzramconfig`/`nvpmodel`/`systemd-modules-load` 全 `[FAILED]`、docker 报 `dm thin-pool` 不可用。**真因**：亚博把引导配置写进 eMMC（`APPEND … root=/dev/mmcblk1p1`）→ **引导器加载 eMMC 的厂商内核（4.9.299），根分区却是 TF 卡上的官方 JP46（模块 4.9.253）**。**判据一句话**：`uname -r` 与 `ls /lib/modules` 必须完全相同（模块还有 `vermagic` 校验，差一个小版本也拒绝加载）。**出路**：① 换与 eMMC 内核配套的系统镜像（亚博出厂镜像）② 把 eMMC 刷成官方系统（有砖风险）③ 全内建（无源码，做不到）。详见 `docs/04` §2.0「内核/模块版本错配」。 |
| E-36 | **`parted --script resizepart` 对"挂载中"的分区照样交互问 Yes，`--script` 不生效（2026-10-22 实测）** | **现象**：`parted /dev/mmcblk1 --script resizepart 1 100%` 只打印 `Warning: Partition … is being used. Are you sure you want to continue?` 然后退出，分区一点没变；再加 `100%` 还会被当成对告警的回答，报 `invalid token: 100%`。**解法**：喂两行输入 + 用结束扇区（不是百分比）：`printf 'Yes\n62333918s\n' | sudo parted ---pretend-input-tty /dev/mmcblk1 resizepart 1`，再 `sudo resize2fs /dev/mmcblk1p1`（ext4 在线扩容，不用重启）。**教训**：**parted 的"分区在用"确认只有 `---pretend-input-tty` 能喂**；百分比在交互路径里容易和告警回答抢位置，直接给扇区最稳。 |
| E-37 | **串口当命令行通道：长命令会丢字符，丢掉引号后 bash 卡在 `>` 续行（2026-10-22 实测）** | **现象**：通过 COM16 发一条 ~150 字符的命令，末尾的 `"` 丢了 → bash 认为引号未闭合，提示符变成 `>` 等续行，后续命令全部被吞。**解法**：① 结束标记**不要用引号**（用 `M=JS123456; echo ${M}END$?` 这种变量拼接，回显里只有 `${M}` 不会误匹配）；② 命令尽量短、拆成多条；③ 卡住时先补一个 `"` 再回车，`Ctrl-C` 兜底。**通用结论**：串口只适合**短命令 + 一次性脚本**，长逻辑用 `pscp` 传脚本再跑。 |
| E-38 | **板子没有出口：Windows ICS 显示 `192.168.137.1` 但 NAT 没生效；策略代理是白名单（apt/github/pypi 全 403）（2026-10-22 实测）** | **现象**：PC ↔ 板子直连通（`ping` 3ms、SSH 通），但板子 `ping 223.5.5.5` 100% 丢包。`SharedAccess` 服务是 `Running`、共享适配器地址也是 `192.168.137.1`，但 `Get-NetNat` 无实例 → **NAT 根本没建**。另试把 PC 的策略代理反推到板子（`plink -R 18080:127.0.0.1:7752`）：隧道通，但 `archive.ubuntu.com`/`github.com`/`pypi.org`/`developer.download.nvidia.com` **全部 403**。**解法**：要么网线插路由器 LAN 口（DHCP 直出），要么在 PC 上重启/重设 ICS（`Restart-Service SharedAccess`，或 `ncpa.cpl` → WLAN 属性 → 共享 → 勾选并选以太网）。**教训**：**"能看到对端"不等于"能出网"**；判断出口只看一条：板子上 `ping 223.5.5.5` + `getent hosts www.baidu.com`。 |
| E-39 | **"eMMC 版本查不出来"是伪命题：rootfs 里根本没有版本文件，只能读 `boot/Image` 的 `Linux version` 字符串（2026-10-23 实测定版）** | **现象**：`/mnt/emmc/etc/nv_tegra_release`、`/etc/os-release`、`/lib/modules` **全都不存在**，于是上午的结论写成"查不出来"。**真因**：eMMC 上只有 `boot/ media/ root/ var/ + README.txt`，`README.txt` 写着 `Download and extract the sample filesystem to this directory.` —— 这是 `Linux_for_Tegra/rootfs/` **解压 sample rootfs 之前**的占位目录，即 eMMC 只刷了 BSP（cboot + 内核 + dtb），从来没装过系统。**解法**：`sudo mount -o ro /dev/mmcblk0p1 /mnt/emmc` 后 `strings /mnt/emmc/boot/Image \| grep 'Linux version'` → `4.9.299-tegra (buildbrain@mobile-u64-5333-d8000) … #1 SMP PREEMPT Tue Nov 22 09:24:39 PST 2022` = **L4T 32.7.3 / JetPack 4.6.3**。**教训**：**BSP 骨架是"版本只在 Image 里"的典型形态**；见到那句 README 就别再找 os-release 了。 |
| E-40 | **SD 卡认不认，由 dtb 决定，不是内核版本；判据是 `dmesg \| grep sdhci-tegra` 里有没有 `.0/.1`（2026-10-23 实测）** | **现象**：把 eMMC 内核换成 4.9.253 后怎么都起不来，`dmesg` 里**只有 `sdhci-tegra.3`（eMMC）**，`mmcblk1` 从头到尾不出现 → 掉进 initramfs `bash-4.4#`。**真因**：当时的 `-user-custom.dtb` 被换成了官方 b00 dtb（237113），那份**没有 SD 控制器节点**；这块板上 SD 能不能枚举看 dtb，跟内核是 4.9.253 还是 4.9.299 关系不大。**正确的那份是原配 `kernel_tegra210-p3448-0002-p3449-0000-b00-user-custom.dtb` = 237290 字节（2024-12-20）**，换回去 `mmcblk1` 立刻出现。**判据**：正常启动的 `dmesg` 应同时有 `sdhci-tegra.3`（eMMC）和 `.0/.1`（SD）；只有 `.3` 就是 dtb 错。**教训**：**"内核降级就能配模块"这条路在这块板上是死的** —— 别把 eMMC 的引导链当可随意改的东西。 |
| E-41 | **initramfs 的急救 shell 里 `sync` / `head` / `uname` 都没有；写完盘必须 `umount` 收尾（2026-10-23 实测）** | **现象**：`bash-4.4#` 里 `cp` 完执行 `sync` → `sync: command not found`；接着 `reboot -f` 跳过卸载，改动可能没落盘；`uname -r` 也是 `command not found`（要看内核版本得用 `dmesg \| grep 'Linux version'`，`cat /proc/version` 会因 `/proc` 未挂载失败）。**解法**：`mkdir -p /m; mount /dev/mmcblk0p1 /m; cp …; umount /m; reboot -f` —— **`umount` 会把数据刷到介质**，等价于 sync 且一定有。**教训**：**别默认 busybox 什么都有**；急救 shell 里每一步都要先确认命令存在。 |
| E-42 | **一口气敲多步的复合命令，是"看起来改了但没生效"的头号来源（2026-10-23 实测两次踩）** | **现象**：`mkdir -p /m; mount …; cp /m/boot/custom.bak /m/boot/*user-custom.dtb; sync; reboot -f` 一条长命令，其中 `cp` 没生效，但后面的 `reboot -f` 照跑 → 表现成"改了也没用"，白折腾两轮（还一度误判成 eMMC 文件系统损坏）。**判据**：每次改完**立刻 `ls -l` 核对大小/时间戳**再重启。**解法**：拆成**每行一件事**，逐行看回显；`cp` 之后单独一行 `ls -l` 验证。**教训**：**验证命令必须和修改命令分开、且在重启之前**。 |
| E-43 | **micro-USB gadget 在热重启后半死：能枚举但数据通道不通（2026-10-23 实测）** | **现象**：`usbipd list` 显示 `2-4 0000:0002 未知 USB 设备(设备描述符请求失败)`；后来设备恢复成 `0955:7020`、`COM16` 也回来了，但脚本 `Write` 直接抛 **"信号灯超时时间已到"**（semaphore timeout），`ReadExisting` 永远返回空 → **串口看着在、实际不能收发**。**附带**：`dmesg` 里持续刷 `configfs-gadget gadget: Wrong NDP SIGN` 说明 RNDIS/CDC 这条 function 本身不稳。**解法**：热重启后先做一次 **`Write` 探活**（发一个 `\r` 看有没有回显），不行就拔插 micro-USB 或冷启动；真正可靠的是 **J41 的 USB-TTL 硬件串口（`console=ttyS0`，针脚 6/8/10）**。**教训**：**"端口存在"≠"通道可用"**，串口脚本要有写超时兜底，否则会静默卡死整个调用。 |
| E-44 | **`.deb` 的载荷从 R32.7.x 起是 `data.tar.zst`，Python 标准库解不了（2026-10-23 实测）** | **现象**：自己写 ar 解析器抽 `data.tar.*`，`tarfile.open()` 报 `not a gzip/bzip2/lzma file` / `invalid header`。**真因**：NVIDIA 的 `nvidia-l4t-kernel` 用 **zstd** 压缩载荷，stdlib 没有 zstd。**解法**：把载荷落盘成 `.tar.zst`，交给 **Windows 自带的 `tar.exe`（bsdtar，libarchive 带 zstd）**：`tar -xf x.tar.zst -C dest ./lib/modules/4.9.299-tegra`。已封装成 `scripts/extract_deb.py`（gz/bz2/xz 走内存，zst 走 bsdtar）。**教训**：**"`.deb` 就是 ar + tar.xz"这句话在新版本上不成立**。 |
| E-45 | **往板子传文件：U 盘枚举失败（`error -110`），网线 + `python -m http.server` 才是稳的（2026-10-23 实测）** | **现象**：FAT32 U 盘插上后 `lsblk` 里没有 `sda`，`dmesg` 刷 `usb 1-2.2: device descriptor read/64, error -110` + `unable to enumerate USB device`；而同一时刻 Windows 侧的 `wsl` 也被沙箱挡住（`Wsl/E_ACCESSDENIED`），读卡器 + usbipd 路线在 agent 里根本跑不了。**可用方案**：PC 网口 `192.168.137.1/24`（ICS 留下的）→ PC 上 `python -m http.server 8000 --directory firmware` → 板上 `sudo ip addr add 192.168.137.50/24 dev eth0` + `wget -O /dev/shm/m.tgz http://192.168.137.1:8000/xxx`（**下到 `/dev/shm` 内存盘，别写坏卡**）。**教训**：**传文件优先走网络服务，不要指望"插 U 盘就能用"**；`/dev/shm` 是坏卡时的安全落点。 |
| E-46 | **TF 卡物理损坏的判据：`error -84 / -110` + `Data CRC error` + 报错扇区越来越靠前（2026-10-23 实测，本板当前状态）** | **现象**：`mmcblk1: error -84 transferring data, sector 21623624, nr 1024` + `mmc1: Data CRC error` + `sdhci: REGISTER DUMP (mmc1)`，retry 无效；报错位置从 `sector 21.6M` 一路退到 `4.3M`。**鉴别**：`mmcblk1` **不存在** = 设备树/驱动问题（E-40）；`mmcblk1` 在但读写 **CRC** = 介质问题，两者修法完全不同。**结论**：**换卡**，别在坏卡上做重活（编译、扩容、写大文件）。**教训**：卡是消耗品，**先排除介质再怀疑配置**——这一轮前半段把它误判成 dtb 问题，多花了一个小时。 |
| E-47 | **亚博出厂镜像解压后是 58 GB 的 `.img`，64GB 卡临界（2026-10-23 查证）** | **现象**：`出厂镜像_ROS+AI视觉/TF卡使用镜像/` 下 `JetsonNano_ros_yolo26.zip` 29.3G、`jetson_nano_b01_TF_20250324.zip` 29.9G，解压出的 img **58 GB**；64GB 卡实际可用约 **59.6 GiB**，Etcher 很可能直接报 target too small。**另一处坑**：这套是 **JetPack 4.6.3（模块 4.9.299）**，与 eMMC 天然配套（这是它最大的价值），但 **OpenCV 仍是 4.1.1，没有 YuNet**。**结论**：想要那套现成环境就上 **128GB** 卡；只求跑通就先走「JP46 + 4.9.299 模块包」。 |
| E-48 | **中文路径下 `cv2.imread/imwrite` 静默失败：返回 `None`/`False`，不报错（2026-10-23 实测）** | **现象**：`cv2.imwrite(r"E:\哨兵管家\snapshots\cam_0.jpg", frame)` 返回 **False** 且不抛异常；`cv2.imread` 对含中文的 Notion 导出图直接 `can't open/read file`；同一份代码换纯 ASCII 路径就正常。**根因**：Windows 上 OpenCV 的 `imread/imwrite` 走 **ANSI 代码页**，UTF-8 中文路径打不开；`open()` 没这个问题。**解法**：读用 `np.fromfile` + `cv2.imdecode`，写用 `cv2.imencode` + `open(..., "wb").write` —— 已封装 `vision/imageio.py`。**判据**：`imwrite` 返回 False / `imread` 返回 None，而 `os.path.exists` 为 True。**通用教训**：**"文件存在却读不进来"，先怀疑路径编码，别怀疑文件坏了**。 |
| E-49 | **日志出口不能顶替主出口：`--jsonl` 一开，WS 就不发了（2026-10-23 自己写的设计缺陷）** | **现象**：仿真跑完，JSONL 事件齐全、终端也在打印"上报"，但 `data/sentinel.db` **一条都没有**，假对话节点连连接都没建立。**根因**：`build_sink()` 写成 `if jsonl_path: return JsonlSink(...)` —— **日志出口把网络出口替掉了**。**危害**：日志越"正常"，越掩盖"对端根本没收到"。**解法**：改成 `MultiSink`（tee），WS + JSONL 同时写，任一失败即计失败；并加单测钉死"jsonl 不能替换 ws"。**通用教训**：**观测通道与被观测通道必须分开**，调试输出不允许改变主链路行为。 |
| E-50 | **Haar 在 1280×720 实拍上把墙砖认成脸；且它没有关键点，SFace 无法对齐（2026-10-23 实测）** | **现象**：一张卫生间照片做链路测试，Haar `minNeighbors=5` 稳定检出 4 个"脸"，其中 3 个是墙砖/门框（证据图在 `snapshots/`）；同一张图 `minSize=80` 后**一个真脸都没有**。**含义**：① Haar 只能当"链路通了"的兜底，**不能拿它的召回率评估系统**；② Haar 没有 5 点关键点 → `FaceRecognizerSF.alignCrop` 对不齐 → **开 Haar 时识别必须降级为"全部陌生人"**。**解法**：`detection.backend=auto` 有 YuNet 就用 YuNet；`enroll_faces.py` 在检测器不是 YuNet 时**直接拒绝注册**并说明原因。 |
| E-51 | **沙箱网络策略把所有模型站都 403；"先跑起来"必须允许降级（2026-10-23 实测）** | **现象**：`github` / `raw.githubusercontent` / `pypi` / `huggingface` / `jsdelivr` / `modelscope` 全部 `proxy tunnel … 403`，连 `www.baidu.com` 也不行；`pip install` 同理。而 YuNet（230KB）+ SFace（37MB）本地没有副本。**解法**：① 代码做**双后端降级**（`auto`：有模型用 YuNet，没有用 Haar；识别无模型降级 `none`），保证"没有模型也能跑通链路"；② 提供 `scripts/fetch_face_models.ps1`（在**用户自己的 PowerShell** 里跑，不经沙箱代理）+ 浏览器直链；③ `vision_selftest.py` 明确打印"缺哪个文件、放哪里"。**通用教训**：**别让"缺一个 37MB 的文件"卡住整条链路的验证** —— 先定义降级语义（这里：全部陌生人），再补模型。 |

## 5. 记忆库自身的坑

| # | 坑 | 说明 |
| :--- | :--- | :--- |
| M-1 | **设计先行导致"规划被误写成已完成"** | 本项目文档先于代码，记忆库极易把 `docs/` 里的规划项记成事实 |
| M-2 | ~~项目已建 git 却未建 `.gitignore`~~ | ✅ **已解决**（2026-10-15）：`.gitignore` 已建并验证，模型/数据库/人脸特征/截图均已排除 |

## 相关记忆

- 待验证项 → `05-open-questions.md`
- 部署正文与故障排查手册 → `docs/04-部署与运维.md`（**打印带到现场**）
- 环境事实 → `02-environment.md`
