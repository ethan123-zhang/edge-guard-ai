---
type: project
topic: 项目地图
last_updated: 2026-10-15
---

# 项目地图

## 仓库定位

**哨兵管家** = 基于异构边缘计算与端侧 LLM 的智能家庭迎宾与安防系统。

大门口用 **Jetson Nano** 做人脸识别（熟人/陌生人 + 到访计数）；家中无人时用
**鲁班猫5（RK3588）** 上的端侧 LLM 与来人**多轮对话**问明来意、**拒绝越权请求**，
最后把结构化结果**推送给家庭主人**。

## 当前阶段（**重要**）

> **设计完成 → 两个死亡点已完成一半（V2 ✅ / V1 ❌）。** 已有全套技术文档，**仍未写实现代码**。

| 项 | 状态 |
| :--- | :--- |
| 技术文档 | ✅ 完成（7 份，约 2300 行） |
| 硬件 | ⚠️ 部分到齐（见下） |
| 代码 | ❌ **未开始**（`vision/`/`dialog/`/`master/` 均未创建） |
| 环境验证 | 🟡 **已开始**（2026-10-15）：**V2 ✅ 通过**（板端 RKLLM 可对话）；**V1 ❌ 未开始**（Jetson 人脸识别） |

## 目录

| 路径 | 内容 |
| :--- | :--- |
| `MEMORY.md` + `memory/` | 本记忆库 |
| `docs/` | **技术文档正文**（设计真源，先于代码） |
| `docs/data/` | **实测数据**（`latency.csv` + 分析 README） |
| `board-pkg/` | **待传板子的一切**（2026-10-17 归整）：`rkllm-board-v1.3.1-v2.tar.gz`（传板用）、v1（**打包输入，勿删**）、**`Qwen3-0.6B_W8A8_RK3588.rkllm`（889.8MB，已 gitignore）**、`prompts/`、`src/` |
| `models-rkllm/` | **转换元数据**（`data_quant.json`，**入库**）。模型本体已于 2026-10-17 移到 `board-pkg/` |
| `scripts/` | WSL 转换链路脚本 + `.gitignore` 自检脚本 |
| `pushToGithub.bat` | 推送脚本（`git add . / commit / push origin main`，**结尾有 `pause`**） |
| `memory/03-design-decisions.md` | 关键设计决策与理由（指针 + 摘要） |

> **git 仓库**：远端 `https://github.com/ethan123-zhang/edge-guard-ai.git`（**HTTPS**），分支 `main`。
> ✅ **`.gitignore` 已建**（2026-10-15）：排除模型文件（`.rkllm`/`.onnx`/`.rknn`/`.gguf` 等）、
> **人脸特征 `.npy`**、**访客截图**、**对话数据库 `sentinel.db`**、录音、日志、虚拟环境。
> 已逐条验证：7 项高危文件全部被拦、8 项应入库文件（`docs/`、`memory/`、代码、`docs/data/*.csv`）无误伤。

> `docs/` 是文档正文的**唯一真源**，记忆库不复制其内容，只放指针与摘要。

## 规划的代码结构（**尚未创建**）

```
vision/      # Jetson Nano 侧：检测+识别+上报
dialog/      # 鲁班猫5 侧：ASR/LLM/TTS/状态机/数据库/上报
master/      # 主人端网页看板
data/        # SQLite 数据库
scripts/     # 安装与 systemd 自启
```

## 硬件状态

| 硬件 | 状态 |
| :--- | :--- |
| Jetson Nano B01 | ✅ 有 |
| 鲁班猫5 v1（RK3588） | ✅ 有 |
| USB 摄像头（亚博智能 1080p） | ✅ 有 |
| 音箱（最基础） | ✅ 有 |
| **麦克风** | ❌ **原麦噪声大不可用，必须新购**（选型见 `03`） |
| PIR 红外 | ❌ 未购（可选，用于补光触发） |
| 补光灯 | ❌ 未购（现场必需，建议 USB LED 灯条） |

## 数据流（规划）

```
摄像头 → Jetson(检测+识别+ROI) → WebSocket → 鲁班猫5(判断+对话+写库) → 主人端
                                                   ↓
                                              SQLite 归档
```

**架构关键决策**：所有业务逻辑集中在 RK3588（大脑），Jetson 只做"识别 + 发事件"。
理由：**单人开发**，减少双机联调面积。

## 相关记忆

- 环境与硬件细节 → `02-environment.md`
- 设计决策与理由 → `03-design-decisions.md`
- 已知坑与风险 → `04-pitfalls-and-risks.md`
- 待验证项 → `05-open-questions.md`
