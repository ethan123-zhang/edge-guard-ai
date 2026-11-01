---
type: index
topic: 哨兵管家记忆库索引
last_updated: 2026-11-01
---

# 哨兵管家：当前状态与记忆入口

先读memory/00-maintenance-rule.md。本页只保留状态摘要与指针；项目进度在本库，通用知识正文在aiVault。

## 当前状态与续接（2026-11-01）

- 当前进度真源：[PROGRESS.md](PROGRESS.md)。Jetson已能本地检测与生成事件；完整身份识别和可靠去重仍未通过。
- 最新验收：[连续确认修复验收](docs/连续确认修复验收-2026-11-01.md)。本机与板端151项通过，三个冻结视频全过；现场整体可靠性仍未通过。
- 续接：先补自然遮挡/重入标注与长期稳定性，再推进熟人识别，最后双板联调。[修复前交接](memory/session-logs/2026-11-01-Jetson可靠性修复与公开视频回归.md)只作历史。
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
| 2026-11-01 | memory/session-logs/2026-11-01-Jetson可靠性修复与公开视频回归.md | 最新正式交接与下一步 |
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
