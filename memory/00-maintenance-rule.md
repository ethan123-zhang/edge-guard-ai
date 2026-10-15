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

## 📤 推送纪律（**每次改动后必做，无例外**）

> **人类明确要求（2026-10-15）**：
> **每次对 `E:\哨兵管家` 中的有用文件做了修改，都必须推送到 GitHub。**
> **不推送 = 没做完。** 这不是可选项。

### ✅ 每次会话收尾的固定动作（照做，不要问）

```bat
cd /d E:\哨兵管家
git add .
git status --short          :: 先看一眼，确认没有不该推的东西
git commit -m "<简述本次改动>"
git push origin main
```

> **沙箱注意**：`pushToGithub.bat` 结尾有 `pause`，在自动化环境下**会卡住**。
> agent 执行时**不要调用 .bat**，直接跑上面四条 git 命令。

### 📋 推送前自检（每次都要过一遍）

- [ ] `git status --short` 看过了？
- [ ] **有没有新增的文件夹/文件类型没被 `.gitignore` 覆盖？**（见下节）
- [ ] 有没有大文件（模型/数据集/视频）混进来？
- [ ] 有没有隐私文件（人脸特征/截图/数据库/录音）混进来？
- [ ] `git push` 后核对 `本地 main == origin/main`？

### 判断"有用文件"的口径

| 要推送 ✅ | 不要推送 ❌（应进 `.gitignore`） |
| :--- | :--- |
| `docs/` 技术文档 | 模型文件（`.rkllm`/`.onnx`/`.rknn`/`.gguf`） |
| `memory/` 记忆库、`MEMORY.md` | 数据库 `sentinel.db` |
| 代码（`vision/`/`dialog/`/`master/`） | 人脸特征 `.npy` |
| 脚本（`scripts/`）、`.gitignore` | 访客截图、录音 |
| 实测数据 `docs/data/*.csv` | 日志、虚拟环境、缓存 |
| 配置模板（不含密钥） | 含密钥的真实配置 |

**仓库信息**：

| 项 | 值 |
| :--- | :--- |
| 远端 | `https://github.com/ethan123-zhang/edge-guard-ai.git`（**HTTPS**） |
| 分支 | `main` |

> ⚠️ **踩坑记录（2026-10-15）**：本仓库最初被配置为 **SSH** 远端
> （`git@github.com:ethan123-zhang/edge-guard-ai.git`），推送**必然失败**：
> ```
> git@github.com: Permission denied (publickey).
> ```
> **根因**：本机 `~/.ssh/id_rsa`（指纹 `SHA256:1GLDvLVrVDCJEU8rM+DZ38rYBCCghx2amqe0zCWs2G4`）
> **未添加到 GitHub 账号**，且 ssh-agent 未运行。
> **解法**：改为 HTTPS 远端（与 `AIQuantEval`、`aiVault` 两个仓库一致），推送即成功。
> ```bat
> git remote set-url origin https://github.com/ethan123-zhang/edge-guard-ai.git
> ```
> **结论**：**本机所有 GitHub 仓库一律用 HTTPS 远端**，不要用 SSH
> （本机 `.ssh/config` 里的 Host 项全是局域网板卡，与 GitHub 无关）。

**双库同时改动时，要各自推送**：
- 知识改动 → `E:\Document\Ethan'vault\aiVault\push_ai.bat`
- 进度改动 → `E:\哨兵管家`（上面的四条 git 命令）

---

## 🚫 新增文件夹 ⇒ 必须同步更新 `.gitignore`（**人类明确要求**）

> **人类明确要求（2026-10-15）**：
> **每次新添了有用的文件夹，都要在 `.gitignore` 中加上地址。**

### 为什么必须做

`git add .` 是**无条件全加**的。新建一个目录时，如果它没被 `.gitignore` 覆盖，
目录里的**全部内容会被直接推上公开仓库**——包括模型、数据库、访客照片。
**这类泄漏推上去之后很难彻底清除**（要改写历史）。

### 固定动作（每次新建文件夹后）

1. 判断这个目录是**要入库**还是**要忽略**（口径见上一节的表）；
2. **要忽略** → 在 `.gitignore` 里**加上该目录路径**（写目录名 + 一个 `**` 兜底）；
3. **要入库** → 不用改 `.gitignore`，但确认它**没有**被某条宽泛规则误伤；
4. **实测验证**（不可跳过）：
   ```bat
   git check-ignore -v <新目录>/任意文件      :: 有输出 = 被忽略（对）
   git check-ignore -v <新目录>/任意.py       :: 无输出 = 可入库（对，若这是代码目录）
   ```
5. 把改动连同代码一起 commit + push。

### 当前 `.gitignore` 的覆盖方式（**按类型写，不按目录写**）

为了让"以后新添的文件夹"**自动被覆盖**，`.gitignore` 主要按**文件类型/通用目录名**写，
而不是逐个写死路径。因此以下**新建同名目录会自动生效**，无需改规则：

| 规则 | 覆盖的新目录 |
| :--- | :--- |
| `models/`、`vision/models/`、`dialog/models/` | 任何模型目录 |
| `*.rkllm` `*.onnx` `*.rknn` `*.gguf` `*.pt` `*.bin` 等 | 任何位置的模型文件 |
| `*.npy` `*.npz`、`vision/face_db/`、`face_db/`、`face_gallery/` | 人脸特征 |
| `*.jpg` `*.jpeg` `*.png` `*.bmp` `*.webp`、`snapshots/`、`captures/` | **任意位置**的图片（访客照片） |
| `*.wav` `*.mp3` `*.pcm` `*.flac`、`recordings/`、`recordings_raw/`、`audio_cache/` | 录音 |
| `data/*.db`、`*.sqlite` | 数据库 |
| `datasets/`、`calib_images/`、`calibration/`、`*.zip` `*.tar*` `*.7z` `*.rar`、`*.bag` `*.rosbag` `*.pcd` `*.ply`、`*.h5` `*.hdf5`、`*.pkl` `*.pickle` | 数据集与标定图（体积大） |
| `logs/`、`*.log`、`__pycache__/`、`venv/`、`.venv/` | 运行产物 |
| `backups/`、`demo_videos/`、`*.mp4` | 备份与演示视频 |
| `temp/`、`scratch/`、`experiments/output/` | 临时实验 |

> **要入库图片怎么办**：把图片放到 `docs/images/`，`.gitignore` 里有
> `!docs/images/**` 例外规则。**其他位置的图片一律不入库。**
>
> ⚠️ **仍需手工加的例外**：上表覆盖不到的自定义目录名，
> 且里面装大文件或隐私数据时，必须手工补一条。
> 例如：`my_photos_2026/`、`face_records/`、`visitor_data/` 这类命名。

### 🔧 自检工具（**每次推送前跑一次**）

已提供脚本，一条命令检出"该忽略却没被忽略"的文件：

```bash
# 用 Git 自带的 bash 运行（本机 WSL 的 bash 不可用，见下方踩坑）
"D:\Program Files\Git\bin\bash.exe" "E:\哨兵管家\scripts\check_gitignore.sh"
```

脚本做四件事：
1. 检查敏感文件**是否已被 git 跟踪**（已跟踪的忽略规则无效）
2. 扫描**未被忽略的可疑文件**（下次 `git add .` 会推上去的）
3. 列出当前所有目录，提示复核覆盖情况
4. 核对与远端的同步状态（有无未推送提交）

退出码 `0` = 安全可推；`1` = 有问题需先处理。

> ⚠️ **本机 bash 踩坑**：`bash` 命令会走 WSL，而 WSL 报
> `execvpe(/bin/bash) failed`（且 localhost 转发失败）。
> **必须用 Git 自带的 `D:\Program Files\Git\bin\bash.exe`**，不要直接用 `bash`。

### ⚠️ `.gitignore` 的生效边界（易错）

**`.gitignore` 只对「未跟踪」文件生效。** 若某文件**已被 `git add` 过**，
之后再写忽略规则**拦不住**，必须显式移除跟踪：

```bat
git rm --cached <文件或目录>     :: 从索引移除，保留本地文件
git commit -m "chore: 停止跟踪 <xxx>"
```

**所以正确顺序是：先建 `.gitignore` 规则，再放文件。** 反过来就要多一步补救。

### ✅ 本项目 `.gitignore` 已生效（2026-10-15 建立）

以下内容**体积大或含隐私**，**均已被 `.gitignore` 排除**，不会随 `git add .` 推上去：

| 路径 | 原因 |
| :--- | :--- |
| `*.rkllm` / `*.onnx` / `*.rknn` / `*.gguf` 等 + `models/` | 模型文件，数百 MB~数 GB |
| `data/sentinel.db` + `data/*.db` | 含人脸与对话记录，**隐私** |
| `vision/face_db/` + `*.npy` | 人脸特征向量，**生物特征隐私** |
| `snapshots/` / `*.jpg` / `*.wav` | 访客截图与录音，**隐私** |
| `*.log` / `logs/` / `venv/` / `__pycache__/` | 运行产物 |

> **注意**：`.gitignore` 只对**未跟踪**文件生效。若某个大文件已被 `git add` 过，
> 需要 `git rm --cached <file>` 才会真正移除跟踪。**新增模型/数据目录时，先确认是否被覆盖**。

## 自我检查（每次写入后）

- [ ] 索引里没有正文？
- [ ] `MEMORY.md` 行数 / 体积在软上限内？
- [ ] 这个主题已有文件、我是在更新而不是新增？
- [ ] 有没有留下与旧结论矛盾的段落？
- [ ] 有没有抄 git/README 已有的易得内容？
- [ ] 有没有把"规划"写成"已完成"？
- [ ] 有没有写入任何秘密？
- [ ] **本次有没有新建文件夹？有 → `.gitignore` 加了吗？验证了吗？**
- [ ] **本次改动推送了吗？`本地 main == origin/main` 核对了吗？**
