---
type: index
topic: 哨兵管家记忆库索引
last_updated: 2026-11-12
---

# 哨兵管家：当前状态与记忆入口

先读memory/00-maintenance-rule.md。本页只保留状态摘要与指针；项目进度在本库，通用知识正文在aiVault。

## 当前状态与续接（2026-11-12）

- **2026-11-12 第二十七轮：换网 + 第一次真机彩排**。板子换网后 IP 变 **Jetson `10.32.174.4` / 鲁班猫 `10.32.174.19`**，旧 `192.168.5.x` 全废；Jetson `board.yaml` 两处地址已改（板上备份 `board.yaml.bak-20261112`），手机地址与 token 已换（`clients=1`）。现场注册 `p_10001 / 队友A`（5/5 帧，框 326–329 px）；来意词表补「送菜 / 送点菜」并部署（`pytest master` **51 passed**）；清库后用例 1「你好」**0/0**、用例 6「我来送点菜」**1/0**（来意=送货，但身份被判陌生人）。摄像头重启后一度丢失（`/dev/video*` 空、视觉崩溃循环），插 USB 即恢复。交接 [彩排与实收第一次](docs/handoffs/2026-11-12-彩排与实收第一次.md)。
- **下一轮先解**：① **陌生人 5 例**（需第二名未注册队友）；② **手机锁屏实收**（无看板 / 有看板两态 + 触发→显示计时）；③ **识别阈值边缘**：同一人 `confidence` 0.351–0.736，阈值 `match_threshold=0.363` → 去演示机位（1.5–2 m）重采注册样本后再定阈值；④ 黑帧保护复验（遮镜头 10 s 不出事件）；⑤ 麦克风未到，语音线未做。
- **2026-11-11 第二十五轮：演示链路全通 + 双板 systemd 自启**。企业微信「群机器人」新版已改名「消息推送」，Webhook 配进板端 `config/master.local.yaml` 后板端直连 `HTTP 200 errcode 0`，真实接待 outbox `report:v_0041` → `sent`。手机看板 `clients:1`、走一次 = 1 条到访 + 1 张方向正确照片（清库后 v_0001）。两板真实 `reboot`：服务自动 `active`、20:05 自动上报、手机自动重连。仓库无代码改动（PushPlus 试做已回滚，`pytest master` 49 passed）。交接 [手机看板与双板自启动](docs/handoffs/2026-11-11-手机看板与双板自启动.md)。**服务日志改看 `journalctl -u sentinel-{master,vision}`**。

## 历史（2026-11-08）

- **2026-11-08 第十六轮**：业务层 10 分钟到访窗口落地 —— `dialog/visits.py`（`VisitBook.arrive`，固定窗口锚在 `arrived_at`、活跃到访不拆、`unk_*` 不互并、`msg_id` 幂等）+ `visit_event` 账本表 + `scripts/visit_window_replay.py`。验收：`dialog.run_tests` 75 项、`vision.run_tests` 353 项全过；证据 [visit-window-2026-11-08.json](docs/data/visit-window-2026-11-08.json)（合成 11 项断言全过；真实自然姿态夹具 12 条 `unk_*` 事件 → 12 次到访，**业务层不跨匿名 ID 猜同人**，属层级边界不是回归）。说明见 [业务到访窗口](docs/业务到访窗口-2026-11-08.md)。仍未接线对话节点（仓库还没有 dialog 主循环），离场判定另立合同。
- **SFace engine 重建分支实跑通过**（2026-11-08，补上一轮只验 YuNet 的空白）：`/tmp` 里目标 engine 不存在 → 内置 profile `min1/opt4/max8×112×112` → trtexec 重建 **483 s** → sidecar 生成并复验；batch 1/4/8 与生产 engine 余弦 ≥ **0.99955**、max abs diff **0.0156**；生产 engine 前后 sha256 未变。**板端 engine/ONNX 在部署根目录 `~/sentinel-quality-20261106/`，不在 `vision/models/`**（那里只有原始模型）。证据 `docs/data/sface-engine-rebuild-2026-11-08.json`。
- **板子地址 2026-11-08 起是 `jetson@192.168.3.154`**：`10.77.156.4` 能 ping 但 SSH 被 reset，**已被别的设备占用**；`scripts/deploy_board.ps1` 默认 `-Host` 已改成新地址。板端三项自检（video0 / ensure_engines --check / check_backend）11-08 均通过。
- 视觉侧可观测性：关联层抑制日志补了 `理由`（`track_bound` 不再只打「同类 — / 异类 —」），新增 1 项单测。
- 2026-11-06 记录：板端腾空间（第十轮）：Jetson 已用 **45G→22G、空闲 31G（87%→42%）**，`/home/jetson` 19G→1.1G。删了 2022–2023 出厂/亚博教程（jetson-inference/librealsense/opencv 源码/ROS ws/torch+tensorflow）与 Docker 里 3 年前 5.61G 的 ros2-base 镜像；保留 `/usr` 系统栈、pycuda+numpy、`~/sentinel-quality-20261106`、测试模型与视频。sudo-only 残留（CUDA deb 源 1.1G 等）见交接。
- 下一轮入口（明天）：**P0 解码先验预计算**，做成 `vision/yunet_decode.py`（先验表缓存，<5 ms，40 帧 parity ≤0.0001 px），验收与命令见[板端存储清理与明日入口](docs/handoffs/2026-11-06-板端存储清理与明日入口.md)。P3 实时延迟被硬件卡住：板上无 `/dev/video*`，需先插 USB 摄像头。
- 最新实验：[TensorRT 封装](docs/TensorRT封装-2026-11-06.md)：`vision/trt_backend.py` 做 sidecar 哈希绑定（engine/模型 sha256、TRT 版本、profile）、动态批量、不可用回退 CPU；板端 40 帧回归检出 69 vs 69、bbox 差 0.0001 px、特征余弦最小 0.999979；检测 83.8→45.9 ms、SFace 218→35.8 ms。发现旧 `decode()` 每帧重建 5875 先验 ≈31 ms，是检测段瓶颈。证据 `docs/data/trt-wrapper-regression-2026-11-06.json`。
- 上一轮：[Jetson TensorRT 加速评估](docs/Jetson-TRT加速评估-2026-11-06.md)：板端 cv2 未编 CUDA，检测/SFace 走 CPU（84/219 ms）；TRT FP16 后 11.2/18.1 ms，单帧约 309→35 ms（8.8×），YuNet 最大差 7.5e-6、SFace 余弦 0.99999。坑：YuNet ONNX 声明 120×160 实际喂 320；pycuda 只存 int 指针会被 GC。证据 `docs/data/trt-acceleration-2026-11-06.json`。
- 上一轮：[合成场景关联验收](docs/合成场景关联验收-2026-11-06.md)：用 HF LFW 同人/异人配对拼三段受控片段（替换/相邻/恢复），跑检测→SFace→`FaceTracker`→`AnonymousAssociator`（τ=0.45、δ=0.10）。三段全过：替换 2 会话不继承、相邻 2 会话不互占、恢复 `own_best=0.9851` 关联回原会话。发现旧版解码器大脸先验 5×5 网格（步长 64），固定位置放脸可能整帧漏检。证据 `docs/data/scenario-association-eval-2026-11-06.json`。
- 前一轮：[板端复验](docs/质量筛选与保守参考策略-2026-11-06.md)：Jetson（3.6.9/4.1.1）重跑同一哈希固定视频，`rms_norm≤0.10` 仍只保留 25 张脸且同侧最低 > 异侧最高（0.5724/0.2239），参考库 16 左 + 5 右，τ=0.45 覆盖 97.8%、歧义 0；多出的 1 张负边距脸被 δ=0.10 拒绝。证据 `docs/data/quality-board-rerun-2026-11-06.json`。板端 numpy/cv2 需 `OPENBLAS_CORETYPE=ARMV8`。
- 更早：[LFW 独立配对验证](docs/质量筛选与保守参考策略-2026-11-06.md)：从 Hugging Face 取 500 同人 + 500 异人配对，AUC 0.9939 / EER 1.6%，异人最高余弦 0.353，τ=0.36–0.60 FAR 均 0；质量门在正脸图像无增益。据此把候选操作点从 τ=0.60 修订为 τ=0.45/δ=0.10（视频覆盖 95.6%→98.4%，错关联仍 0），仍待场景素材验收。
- 早期：[完整视频隔离评估](docs/完整视频隔离评估-2026-11-06.md)：两端完整解码1719帧、跑完287采样点；板端左245/右248、两侧同时234、无检出28，493份有效128维特征，处理窗口83.163秒；同侧最低余弦0.04036低于异侧最高0.39788，**单一全局阈值不能冻结**。本机191项回归通过；生产关联未启用、整体可靠性仍未通过。
- 上一次实验：[旧版YuNet隔离链路](docs/旧版YuNet隔离链路验收-2026-11-01.md)通过四帧执行验证，无系统升级；完整287点覆盖已于11-06补齐。

- 关联层核心已实现但默认关闭：`vision/association.py`（τ=0.45、δ=0.10、入册 rms_norm≤0.10），20 项单测；尚未接入 `VisionService`，事件协议不变。
- TRT 封装已实现但默认关闭：`vision/trt_backend.py`（哈希绑定 + 动态批量 + 回退 CPU），17 项单测；板端回归通过，尚未接入 `VisionService`；接入前先把 decode 先验预计算（当前约 31 ms/帧）。
- 当前进度真源：[PROGRESS.md](PROGRESS.md)。Jetson已能本地检测与生成事件；完整身份识别和可靠去重仍未通过。
- 最新验收：[自然姿态本地事件](docs/自然姿态本地事件验收-2026-11-01.md)预期2实际12，未通过；[600秒压力](docs/板端稳定性验收-2026-11-01.md)通过，本机/板端191项通过。整体可靠性仍未通过。
- 续接：用户已确认[保守关联合同](docs/跨轨关联与两层去重合同-2026-11-01.md)。**下一个对话继续人脸识别提速**：先做解码先验预计算（约 31 ms/帧 → 目标 <5 ms），再接 `VisionService`（默认 CPU、`auto` 失败回退），见[提速交接](docs/handoffs/2026-11-06-人脸识别提速交接.md)。合成场景（替换/相邻/恢复）已通过；仍缺真实门口素材与歧义用例，之后才接关联层。自然姿态12条失败保留，不得以延长TTL或减少事件数宣告修复。
- 本次正式收尾：[板端存储清理与明日入口](docs/handoffs/2026-11-06-板端存储清理与明日入口.md)（上一份：[人脸识别提速交接](docs/handoffs/2026-11-06-人脸识别提速交接.md)）。
- 环境与目录以交接及最新结构化证据为准；旧日期记录保留为历史，不冒充当前复验。

## 项目定位

Jetson负责门口视觉，RK3588负责规则、对话与归档。完整目标仍含熟人记忆、越权拒绝和离线可用；本轮仅验收Jetson本地采集、降级检测及事件路径。

## 记忆主题

| 文件 | 内容 |
| --- | --- |
| memory/00-maintenance-rule.md | 双库分工与维护纪律 |
| memory/01-project-map.md | 项目结构及历史阶段 |
| memory/02-environment.md | 当前环境事实；旧画像按日期区分 |
| memory/03-design-decisions.md | 既有设计决策指针 |
| memory/04-pitfalls-and-risks.md | 历史坑位与风险 |
| memory/05-open-questions.md | 当前未通过与待验证项目 |
| memory/06-push-and-gitignore.md | Git与隐私数据忽略规则 |
| memory/07-network.md | 历史拓扑；IP须按现场重新确认 |

## 会话记录与验收证据

| 日期 | 文件 | 用途 |
| --- | --- | --- |
| 2026-11-06 | docs/handoffs/2026-11-06-板端存储清理与明日入口.md | 板端腾空间结果、P0–P3 估时、明日入口 |
| 2026-11-06 | docs/data/lfw-verification-eval-2026-11-06.json | LFW 独立配对验证结构化证据 |
| 2026-11-06 | docs/handoffs/2026-11-06-完整视频隔离评估.md | 本次正式交接与续接入口 |
| 2026-11-01 | memory/session-logs/2026-11-01-Jetson连续确认与稳定性验收收尾.md | 上一次正式交接（历史） |
| 2026-11-01 | memory/session-logs/2026-11-01-Jetson可靠性修复与公开视频回归.md | 连续确认修复前的历史交接 |
| 2026-10-31 | memory/session-logs/2026-10-31-Jetson单板验收与事件诊断.md | 上一轮历史交接 |
| 2026-10-31 | docs/Jetson事件链路诊断-2026-10-31.md | 最新同帧诊断与额外轨道结论 |
| 2026-10-31 | docs/Jetson单板验收-2026-10-31.md | 第一阶段与补充真人测试历史 |
| 2026-10-31 | docs/data/jetson-2026-10-31-event-diagnosis.json | 最新结构化结论 |
| 2026-10-31 | docs/data/jetson-2026-10-31-acceptance.json | 第一阶段数据及后续轮次指针 |
| 2026-10-23 | memory/session-logs/2026-10-23c-PC视觉仿真.md | PC仿真历史，不替代板端结果 |
| 2026-10-23 | memory/session-logs/2026-10-23b-eMMC版本实测与内核模块同步.md | 历史内核/模块与TF故障背景 |
| 2026-10-17 | memory/session-logs/2026-10-17d-PC模型测试与无状态架构.md | 历史LLM架构实验 |

其他旧会话按日期保存在memory/session-logs/，不删除；需要时按主题检索。

## 技术文档真源

设计正文：docs/README.md及docs/01至07。原始日志与逐帧元数据：logs/jetson-20261031/（已忽略，不入Git）。模型、人脸特征、截图、数据库和录音均保持忽略。

## aiVault知识指针

根目录：E:\Document\Ethan'vault\aiVault。

- wiki/concepts/Edge_Vision_Pipeline_Validation.md：同帧、分层、时序和旧版Python验收方法。
- wiki/comparisons/Jetson_Haar_Probe_vs_Production.qmd：本次Haar两路实验、计时边界及解释限制。
- wiki/concepts/Dual_Store_Knowledge_vs_Project_Memory.md：知识与进度分库约定。
- wiki/concepts/Edge_LLM_Model_Selection_Latency_Budget.md：既有端侧LLM延迟分析。

## 固定协作约定

Codex规划与独立验收，DeepSeek限定实现；先定义接口、范围和测试。任务卡保留【输入输出定义】【核心算法逻辑伪代码】【异常与边界条件】【验收测试用例】。

设备凭据不保存到记忆或任务卡。Git保存前检查实际范围和隐私规则，不提交无关文件、原始图像或认证信息。
