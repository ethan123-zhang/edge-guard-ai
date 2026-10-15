---
type: rule
topic: 记忆维护纪律
last_updated: 2026-10-15
---

# 记忆维护纪律（长期遵守）

本文件是本记忆库的**唯一规则源**。任何 agent 读写记忆前先读这里。

## 硬性纪律

1. **`MEMORY.md` 只做索引**——每个主题一行（编号 / 主题 / 文件 / 一句话）。
   **正文一律写进 `memory/` 子文件**，索引里不堆正文。
2. **软上限 150 行或 20KB**，**永远不要逼近 200 行 / 25KB 的加载上限**。
3. **每个子文件只保存一个明确主题**。一个文件混两件事就拆开。
4. **写入前先检索现有记忆**——能更新旧记录，就**不要重复新增**。
5. **新结论推翻旧结论时，直接修正旧记录**，不保留互相矛盾的两个版本。
6. **Git / 代码 / 项目文档中已明确存在且容易重新获得的内容，不重复抄进记忆**。
   记忆只放：结论、判断、踩坑经验、以及"重新获得成本高"的东西。
7. **密码、密钥、Cookie、Token、私钥等一切秘密，永不写入记忆。**
8. **按类型分类**：`user` / `feedback` / `project` / `reference`。

## 目录约定

```
E:\哨兵管家\
├── MEMORY.md                      # 索引 + 当前状态板（唯一入口）
├── docs\                          # 技术文档（设计正文，先于代码存在）
│   ├── README.md
│   ├── 01-总体设计.md
│   ├── 02-接口协议.md
│   ├── 03-对话系统设计.md
│   ├── 04-部署与运维.md
│   ├── 05-演示与答辩.md
│   └── 06-实施计划.md
└── memory\
    ├── 00-maintenance-rule.md     # 本文件
    ├── 01-project-map.md
    ├── 02-environment.md
    ├── 03-design-decisions.md
    ├── 04-pitfalls-and-risks.md
    └── 05-open-questions.md
```

> 本项目**未启用**按类型分目录（`user/feedback/project/reference`），
> 因为 DSH 实现不提供记忆类型机制，强行建空目录违反纪律 8。
> 各文件的 `type:` frontmatter 字段承担同样的分类作用。

## 🔑 双库分工（重要，防止知识分叉）

> 这套约定是**跨项目通用**的，完整论述已沉淀在 aiVault：
> `E:\Document\Ethan'vault\aiVault\wiki\concepts\Dual_Store_Knowledge_vs_Project_Memory.md`
> （本节是它在**本项目**的落地实例，通用原则以 aiVault 那页为准。）

| 库 | 位置 | 存什么 | 不存什么 |
| :--- | :--- | :--- | :--- |
| **aiVault**（知识真源） | `E:\Document\Ethan'vault\aiVault` | 知识正文：机理、原理、方案对比、实测数据、来源快照 | 项目进度 |
| **项目记忆库**（本库） | `E:\哨兵管家` | **进度**：做了什么、接下来做什么、踩过的坑、未验证项 | 知识正文（只放指针） |

**规则**：

1. **知识写 aiVault，进度写本库。** 本库**不复制** aiVault 的正文，
   只放结论摘要（1–2 行）+ 指向 aiVault 对应页面的路径。
2. 一旦发现同一知识在两边都有完整正文，**以 aiVault 为准**，删掉本库的副本。
3. aiVault 有自己的规则（`aiVault/AGENTS.md`）：改完必须更新其 INDEX/LOG 并跑
   `push_ai`。**本库的规则不能替代它**。
4. 本库与 aiVault **各自独立 git**，推送互不影响。

### 📂 aiVault 内部该落哪个目录

> **完整分流表与判据见 aiVault 通用约定页**（避免两处维护）：
> `aiVault/wiki/concepts/Dual_Store_Knowledge_vs_Project_Memory.md` §4
> 权威模板在 `aiVault/prompts/ingest.md` §4。

一句话记忆：**机理→`concepts/`、选型对比→`comparisons/`、工具用法→`entities/`、
数据溯源→`raw/`**；一次分析产出多种就**分开落不同目录**。

**本项目已建的落点**：

- 端侧 LLM 选型与延迟预算 → `wiki/concepts/Edge_LLM_Model_Selection_Latency_Budget.md`
- RK3588 芯片资料 → `wiki/entities/RK3588.qmd`（既有，他人所建）
- 双库分工约定 → `wiki/concepts/Dual_Store_Knowledge_vs_Project_Memory.md`（既有）

## ⚠️ 本项目的重要偏差：设计先行

本项目**技术文档（`docs/`）写在代码之前**，且目前**尚无任何代码**。
这与一般"先做后记"的项目不同，记忆库里要如实反映这一状态，
不要把设计文档中的**规划项**写成**已完成项**。

## 关于本记忆库的性质

- 本记忆库是**人工维护的普通 Markdown 文件**，**不是** DSH 内置的自动记忆功能。
- 因此这些文件**不会被自动加载**，只在人或 agent 主动读取时生效。

## 📤 推送纪律（**本项目当前未启用**）

> ⚠️ **现状**：`E:\哨兵管家` **不是 git 仓库**（无 `.git`、无 `push_ai.bat`）。
> `E:\AIQuantEval` 那套 `push_ai.bat` 纪律在本项目**尚不适用**。

**待办**：若要启用推送纪律，需先完成——
1. 在 `E:\哨兵管家` 执行 `git init` 并关联远端；
2. 建 `push_ai.bat`；
3. 补 `.gitignore`（**务必排除模型文件 `.rkllm`/`.onnx`、`data/sentinel.db`、
   截图、人脸特征 `.npy`**，这些体积大或含隐私，不应入库）；
4. 启用后回写本节，把"未启用"改为实际命令。

**在此之前**：改完记忆库**无需也无法推送**，但要在回复里明确告知未推送。

## 自我检查（每次写入后）

- [ ] 索引里没有正文？
- [ ] `MEMORY.md` 行数 / 体积在软上限内？
- [ ] 这个主题已有文件、我是在更新而不是新增？
- [ ] 有没有留下与旧结论矛盾的段落？
- [ ] 有没有抄 git/README 已有的易得内容？
- [ ] 有没有把"规划"写成"已完成"？
- [ ] 有没有写入任何秘密？
- [ ] 推送状态是否已在回复中如实说明？
