---
type: rule
topic: 推送与 .gitignore 纪律
last_updated: 2026-10-15
---

# 推送与 .gitignore 纪律（**每次改动后必做，无例外**）

> 本文件从 `00-maintenance-rule.md` 拆出（该文件曾超 200 行硬上限）。
> 记忆库的**写入纪律**见 `00-maintenance-rule.md`；本文件只管**推送与忽略**。

## 硬性要求

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

> ⚠️ **本机 bash 踩坑（2026-10-15 补充）**：
> ① 直接用 `bash` 会走 WSL，报 `execvpe(/bin/bash) failed`（且 localhost 转发失败）；
> ② **在 DSH 沙箱内**，Git 自带的 `bash.exe` 也会失败：
> `fatal error - couldn't create signal pipe, Win32 error 5`
> （沙箱下无法创建命名管道）。**此时改用 PowerShell 做等效检查**：
> ```powershell
> git status --short
> git ls-files | Select-String -Pattern '\.(rkllm|onnx|npy|db|jpg|wav)$'   # 应为空
> ```
> 见 `04-pitfalls-and-risks.md` 的 E-9。

### ⚠️ `.gitignore` 的生效边界（易错）

**`.gitignore` 只对「未跟踪」文件生效。** 若某文件**已被 `git add` 过**，
之后再写忽略规则**拦不住**，必须显式移除跟踪：

```bat
git rm --cached <文件或目录>     :: 从索引移除，保留本地文件
git commit -m "chore: 停止跟踪 <xxx>"
```

**所以正确顺序是：先建 `.gitignore` 规则，再放文件。** 反过来就要多一步补救。

### ✅ 本项目 `.gitignore` 已生效（2026-10-15 建立并验证）

以下内容**体积大或含隐私**，**均已被 `.gitignore` 排除**，不会随 `git add .` 推上去：

| 路径 | 原因 |
| :--- | :--- |
| `*.rkllm` / `*.onnx` / `*.rknn` / `*.gguf` 等 + `models/` | 模型文件，数百 MB~数 GB |
| `data/sentinel.db` + `data/*.db` | 含人脸与对话记录，**隐私** |
| `vision/face_db/` + `*.npy` | 人脸特征向量，**生物特征隐私** |
| `snapshots/` / `*.jpg` / `*.wav` | 访客截图与录音，**隐私** |
| `*.log` / `logs/` / `venv/` / `__pycache__/` | 运行产物 |

> **实测验证记录（2026-10-15，2026-10-17 复验）**：889.8MB 的 `Qwen3-0.6B_W8A8_RK3588.rkllm`
> `git check-ignore -v` 命中 `.gitignore:8:*.rkllm`。**规则按文件类型全局生效，
> 所以 2026-10-17 把模型从 `models-rkllm/` 移到 `board-pkg/` 后依然被排除，无需改规则。**
> `git status --untracked-files=all` 里看不到任何 `rkllm` 字样（已复验）。
>
> 📌 **移动/新增模型目录后的检查动作**：跑
> `git status --porcelain -uall | Select-String rkllm`，**结果应为空**。

> **注意**：`.gitignore` 只对**未跟踪**文件生效。若某个大文件已被 `git add` 过，
> 需要 `git rm --cached <file>` 才会真正移除跟踪。**新增模型/数据目录时，先确认是否被覆盖**。

## 相关记忆

- 记忆写入纪律 → `00-maintenance-rule.md`
- 环境相关的坑 → `04-pitfalls-and-risks.md`
