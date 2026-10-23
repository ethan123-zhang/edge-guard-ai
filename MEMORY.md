---
type: index
topic: 哨兵管家 记忆库索引
last_updated: 2026-10-22
---

# MEMORY.md — 哨兵管家 项目记忆索引

> **唯一入口。** 本文件只做索引，不堆正文；正文在 `memory/` 子文件。
> 读写前先读 `memory/00-maintenance-rule.md`（规则源）。

---

## 📍 当前状态板

| 项 | 状态 |
| :--- | :--- |
| **阶段** | **PC 架构验证完成**：带历史多轮不可用 → 已定「**规则 + 状态机 + 每轮无状态 LLM**」；代码部分开始（`dialog/` 3 模块，状态机未写） |
| **代码** | 🟡 `dialog/` 已有 `privilege.py` / `phrases.py` / `db.py` + 46 测试；`state_machine.py` / `main.py` 未写；`vision/` / `master/` 未开始 |
| **模型转换** | ✅ **已完成**：`Qwen3-0.6B_W8A8_RK3588.rkllm`（889.8MB）**仅用 33.6 秒**（RTX 4060 加速） |
| **两个死亡点** | ✅ **V2 已通过**（板端可对话）；✅ **人设注入已板端验证**（2026-10-17 18:21，**仅单轮**；多轮会复读，见 V12/V13）；🟡 **V1 受阻暂停**（Jetson 环境不支持） |
| **阻塞项** | ① 麦克风未购（原有不可用） ② ~~V1 解冻需先问亚博~~ → ✅ **已解决（10-20）** ③ 🔴 **TF 卡物理损坏（`mmcblk1` 持续 `Data CRC error` / `-84`），必须换卡**，见 E-46 ④ ✅ 内核/模块错配**已解决（10-23）**，WiFi 驱动可用，只差连上热点验证出口 |
| **当前主线** | 🟢 **鲁班猫5（RK3588）+ 0.6B**，架构改为「规则前置 + 每轮无状态」；V1 暂时搁置 |
| **新发现（重要）** | ❌ **带历史的多轮 REPL 会让 0.6B/1.7B 复读**；换模型/改提示词都无效（V12/V13）。✅ **规则 + 无状态调用**在 PC 上 **0.6B 8/8 干净**（V14）。详见 `docs/data/pc-model-tests.md` |
| **Jetson 镜像（10-20 / 10-23 定版）** | **eMMC = L4T 32.7.3 / JetPack 4.6.3（内核 `4.9.299-tegra`，2022-11-22 编译）** —— 串口挂 eMMC 后 `strings boot/Image` 实测。⚠️ **官方没有 32.7.3 的 SD 镜像**（32.7.1/32.7.3 都是 4.9.253，32.7.3 只发 BSP）。手上 `jetson-nano-jp46-sd-card-image.zip`（R32.6.1，`sd-blob-b01.img` 12.76G）配 eMMC 会**必然错配**，要补模块包。亚博出厂镜像 `JetsonNano_ros_yolo26.zip`（29.3G）/`jetson_nano_b01_TF_20250324.zip`（29.9G）解压 **58G img** → 需 128G 卡 |
| **性能警告** | ⚠️ 旧方案 824 token 提示词常驻 → generate **31.65 → ~18.6 tok/s（↓43%）**。新方案窄提示约 80–100 token，但**每轮要重 prefill**，需实测（E-25/E-28） |
| **Jetson 首启（10-21/22）** | ✅ **已解决（10-22 晚，离线改卡）**：真因是 JP46 把 **`default.target` 指向 `nv-oem-config.target`**，而 `nv-oem-config-debconf@` 是**模板单元**，tty1 与 ttyGS0 **各跑一个向导** → 抢 `debconf` 锁 → 后到者退出、先到者在看不见的 tty1 上 `no limit` 干等。**所以重烧/换镜像全无效**。用 `scripts/jetson-offline-unlock.ps1`（读卡器→usbipd→WSL2→改 ext4）建账号 + 屏蔽向导 + 放开串口 getty，一次解决。详见 `docs/04` §2.0 |
| **Jetson 现状（10-23 收尾）** | ✅ **内核/模块错配已解决**：往 TF 根分区装了 `lib/modules/4.9.299-tegra`（675 文件/75.2 MB，来自 32.7.3 BSP）→ `modprobe rtl8822ce` 无报错、`wlan0` 出现。🔴 **但 TF 卡物理损坏**（`error -84` + `Data CRC`，报错扇区从 21.6M 退到 4.3M）→ **必须换卡**。另：micro-USB gadget 热重启后半死（能枚举但 `Write` 报"信号灯超时"），当前只能靠 HDMI+键盘。 |
| **Jetson 基线（10-23 实测）** | 板型 = **Jetson Nano B01（官方版，不是 SUB）** · **eMMC = L4T 32.7.3 / JP4.6.3，内核 `4.9.299-tegra`；eMMC 无系统（只有 `boot/ media/ root/ var/ + README.txt` 空壳）** · TF 根分区 = L4T R32.6.1 / Ubuntu 18.04.5 / 模块 `4.9.253`（已补 `4.9.299`）· **`mmcblk0`=14.7G eMMC（引导），`mmcblk1`=TF（根）** · **`boot/extlinux/extlinux.conf` 的 `FDT` = `kernel_...-b00-user-custom.dtb`（237290）—— 这份 dtb 决定 SD 认不认，换成官方 b00(237113) 就没有 `mmcblk1`**（E-40）· OpenCV 4.1.1（`dnn=True`/`YuNet=False`）· CUDA 10.2 · 内存 3963MB |
| **下一步** | ① 🔴 **换 TF 卡**（32GB 起走 B 方案；想要亚博 ROS/YOLO 现成环境则 **128GB**） ② 烧 `firmware/sd-blob-b01.img`（Etcher 开 verify），首启**不插 micro-USB**、向导选**最大分区**（E-34） ③ 网线直连 PC（`192.168.137.1`）→ PC 跑 `python -m http.server 8000 --directory firmware` → 板上 `wget -O /dev/shm/m.tgz http://192.168.137.1:8000/jetson-4.9.299-modules.tar.gz` → `tar -xzf … -C /` → `chown -R root:root /lib/modules/4.9.299-tegra` ④ 连 WiFi（新镜像 `nmcli`；**当前这套 NM 没跑**，只能 `wpa_supplicant`）⑤ 编 OpenCV 4.5.4 → 回答 V1 ⑥ 主线：`dialog/state_machine.py` + `main.py` ⑦ 买麦克风 |
| **交接入口** | **下次开工先读** `memory/session-logs/2026-10-23b-eMMC版本实测与内核模块同步.md` **§8「接下来该做的」**；坑位索引 `04-pitfalls-and-risks.md` 的 **E-39~E-47**；流程细节 `docs/04` §2.0 |

---

## 🎯 项目一句话

大门口 Jetson Nano 做人脸识别（熟人/陌生人 + 到访计数）；家中无人时鲁班猫5（RK3588）
用**端侧 LLM** 与来人**多轮对话**问明来意、**拒绝越权请求**，结果**推送主人**。

**三个核心亮点**：熟人记忆（带历史上下文的问候）· 越权拒绝（**不走 LLM**）· 断网可用。

---

## 📚 记忆文件索引

| # | 主题 | 文件 | 一句话 |
| :--- | :--- | :--- | :--- |
| 00 | 记忆维护纪律 | `memory/00-maintenance-rule.md` | **规则源**：双库分工、写入纪律、自检清单 |
| 01 | 项目地图 | `memory/01-project-map.md` | 仓库定位、当前阶段、目录、硬件状态 |
| 02 | 环境与硬件事实 | `memory/02-environment.md` | 两块板子的约束、开发环境、网络方案、**转换机 GPU** |
| 03 | 关键设计决策 | `memory/03-design-decisions.md` | D1~D15 决策与理由（**指针式**） |
| 04 | 已知坑与风险 | `memory/04-pitfalls-and-risks.md` | P0/P1/P2 坑、流程风险、已识别的设计缺陷 |
| 05 | 待验证与开放问题 | `memory/05-open-questions.md` | **未验证项看板**：V0~V8、开放问题、待采集数据 |
| 06 | 推送与 .gitignore 纪律 | `memory/06-push-and-gitignore.md` | 推送命令、忽略规则、自检（**2026-10-15 从 00 拆出**） |
| 07 | 网络方案事实 | `memory/07-network.md` | 现场拓扑、NAT 约束、**IP 规划值 vs 实测值**、端口（**2026-10-17 从 02 拆出**） |

### 📓 会话记录（流水账，非规则）

| 日期 | 文件 | 主题 |
| :--- | :--- | :--- |
| 2026-10-23 | `memory/session-logs/2026-10-23b-eMMC版本实测与内核模块同步.md` | **实测定版**：eMMC = L4T 32.7.3 / JP4.6.3（从 `boot/Image` 字符串读，rootfs 是空壳）；官方无配套 SD 镜像；**降内核失败的真因是 dtb**（237290 才认 SD）；最终用 32.7.3 模块树解决错配、`wlan0` 复活；TF 卡物理损坏；经验 8 条 + 下一步 |
| 2026-10-23 | `memory/session-logs/2026-10-23-板型纠正与镜像选择.md` | **收尾/交接**：型号纠正（**B01 官方版，不是 SUB**）、镜像选择的决策依据（**eMMC 内核与 TF 模块必须同源**）、11 个问题清单、经验 8 条、**下一步五步走** |
| 2026-10-22 | `memory/session-logs/2026-10-22b-离线修卡与内核错配.md` | **那晚**：debconf 锁真因（`default.target→nv-oem-config.target` + 模板单元双跑）、离线修卡脚本、串口接管、扩分区、**内核/模块错配导致 WiFi 起不来**、卡在没有出口 |
| 2026-10-21 | `memory/session-logs/2026-10-22-Jetson首启卡死与串口救回.md` | **首启卡死定位**：OEM 向导回退到 micro-USB 串口（EDID 坏）；`plink` 乱码→`putty` GUI；向导拔电→账号没建成；`wsl --mount` 不认可移动盘；**已重烧，首启待做**；含「下次开工第一件事」 |
| 2026-10-20 | `memory/session-logs/2026-10-20-Jetson B01 官方镜像.md` | **型号纠正 + 官方 JP46 镜像下载校验**；A02/B01 镜像不可混用；下载踩坑（schannel 不通→Python）；烧写步骤见 `docs/04` §2.0 |
| 2026-10-17 | `memory/session-logs/2026-10-17d-PC模型测试与无状态架构.md` | **夜篇**：PC 实测 0.6B/1.7B，确认多轮复读是结构问题；定「规则 + 状态机 + 每轮无状态」；**含明天上板步骤** |
| 2026-10-17 | `memory/session-logs/2026-10-17c-板端实测与规则引擎.md` | **收尾篇**：板端人设复验数据、**越权实测失败**（V10）、提示词拖慢 43%（V11）；落地 `dialog/` 第一批代码（46 测试全绿）；**下次任务 N-1~N-6** |
| 2026-10-17 | `memory/session-logs/2026-10-17-鲁班猫5提示词补丁.md` | **llm_demo 系统提示词补丁**（**上篇**）：发现官方 `rkllm_set_chat_template()` 入口 + thinking 偷 token；补丁本机验证通过；**待上板实测** |
| 2026-10-17 | `memory/session-logs/2026-10-17b-打包与目录治理.md` | **下篇**：v1/v2 包治理（**v1 是打包输入，勿删**）、PC 侧归整到 `board-pkg/`、**板端 `~/sentinel/` 目录**、传输方式选型 |
| 2026-10-16 | `memory/session-logs/2026-10-16-Jetson环境受阻.md` | **V1 受阻暂停**：Jetson 缺 `cv2.dnn`/无 CUDA/OpenCV 3.2.0；**转回鲁班猫5**；7 个问题与发现；下次任务清单 |
| 2026-10-15 | `memory/session-logs/2026-10-15-模型转换与V2验证.md` | **模型转换链路打通 + V2 死亡点通过**（**上篇**：板端与交付物，P6~P12）；**2026-10-17 拆分** |
| 2026-10-15 | `memory/session-logs/2026-10-15b-模型转换链路.md` | **下篇**：转换链路与环境坑（P1~P5、P11），含复现转换的最短路径 |

> **会话记录只记"这次发生了什么"**，结论与事实已归入 `02`/`04`/`05`，不在此重复。
> **下次开工**：读 `MEMORY.md` 状态板 → 读对应会话记录的「下次任务」节。

---

## 📄 技术文档索引（正文真源在 `docs/`）

| 文档 | 内容 |
| :--- | :--- |
| `docs/README.md` | 文档导航 + 五条铁律 |
| `docs/01-总体设计.md` | 范围边界、架构、数据流、技术选型 |
| `docs/02-接口协议.md` | JSON 消息格式、SQLite 表结构、config.yaml |
| `docs/03-对话系统设计.md` | 状态机、槽位、**越权拦截**、提示词 |
| `docs/04-部署与运维.md` | 环境搭建、RKLLM 部署、**故障排查手册** |
| `docs/05-演示与答辩.md` | **演示脚本**、兜底方案、**答辩问答预案** |
| `docs/06-实施计划.md` | 4 周计划、里程碑、降级预案 |
| `docs/AI门卫联调总结-技术版.md` | **2026-10-17 联调技术复盘**：7 个核心问题、实测数据、提示词演进 |
| `docs/智能门卫值班日记-第一天上岗翻车实录.md` | **同批坑的幽默叙事版**（门卫第一人称，可作答辩素材/科普文） |

> `docs/` 是设计正文的**唯一真源**。记忆库只放指针与摘要，**不复制正文**。

---

## 📤 推送与 .gitignore 纪律（**每次改动必做**）

| 动作 | 命令 / 要求 |
| :--- | :--- |
| **推送** | `git add . && git commit -m "..." && git push origin main` |
| **推送前自检** | `"D:\Program Files\Git\bin\bash.exe" scripts\check_gitignore.sh` |
| **新建文件夹** | **必须在 `.gitignore` 中加上该目录**，然后跑自检验证 |

> ⚠️ **铁律**：① 改了有用文件**必须推送**，不推送 = 没做完；
> ② 新增文件夹**必须确认 `.gitignore` 覆盖**，跑脚本验证，别靠记忆。
> 详见 `memory/00-maintenance-rule.md`。

⚠️ **`.gitignore` 只对未跟踪文件生效** —— 已 `git add` 过的文件要
`git rm --cached` 才能停止跟踪。**正确顺序是先写规则、再放文件。**

---

## 🔗 双库分工（防止知识分叉）

| 库 | 位置 | 存什么 |
| :--- | :--- | :--- |
| **aiVault**（知识真源） | `E:\Document\Ethan'vault\aiVault` | 知识正文：机理、原理、对比、实测数据 |
| **项目记忆库**（本库） | `E:\哨兵管家` | **进度**：做了什么、下一步、踩坑、未验证项 |

**通用约定正文**：`aiVault/wiki/concepts/Dual_Store_Knowledge_vs_Project_Memory.md`

**本项目已沉淀到 aiVault 的知识**：
- `aiVault/wiki/concepts/Edge_LLM_Model_Selection_Latency_Budget.md`
  —— 端侧 LLM 选型与语音对话延迟预算（RK3588 实测 tokens/s 对照）

---

## ⚙️ 五条铁律（速查）

1. **播报时闭麦** —— 否则系统自问自答
2. **卡 2 小时就换方案** —— 一个人做，硬啃是最大时间黑洞
3. **第 3 周冻结功能，第 4 周不改环境**
4. **所有阈值外置到配置文件** —— 现场改配置比改代码快一百倍
5. **数据边测边记** —— 最后三天补不出来

---

## 📤 推送状态

✅ **本仓库是 git 仓库**：远端 `git@github.com:ethan123-zhang/edge-guard-ai.git`，分支 `main`。

| 库 | 推送命令 |
| :--- | :--- |
| **本项目**（进度） | `E:\哨兵管家\pushToGithub.bat` |
| **aiVault**（知识） | `E:\Document\Ethan'vault\aiVault\push_ai.bat` |

✅ **`.gitignore` 已创建并验证**（2026-10-15，见 `memory/06-push-and-gitignore.md`）：
模型文件（`.rkllm`/`.onnx`）、数据库、人脸特征、访客截图、音频均已排除。
**新增目录时仍必须同步加规则并跑自检**，见 `memory/00-maintenance-rule.md` 高危项表。

---

## 维护提示

- 每次写入后过一遍 `memory/00` 末尾的**自检清单**
- **不要把 `docs/` 里的规划项写成"已完成"** —— 本项目设计先行，极易混淆
