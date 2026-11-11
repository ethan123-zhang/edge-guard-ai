# 当前进度与续接入口

## 第二十轮：主人端服务 + Web 看板落地（2026-11-11）

> 交接：[handoffs/2026-11-11-主人端服务实现](docs/handoffs/2026-11-11-主人端服务实现.md)
> · 设计：[主人端-设计与接口](docs/主人端-设计与接口-2026-11-11.md)
> · 证据：[owner-client-2026-11-11.json](docs/data/owner-client-2026-11-11.json)

- **`master/` 包落地**（阶段 2–3 主体，纯标准库）：配置外置 + 三张新表 + `dialog_turn.utterance_id`
  迁移；设备 token 只存 sha256、照片 URL HMAC 短签；`outbox` 稳定键 + 退避重试 + **只扫 outbox 补发**；
  照片 `tmp→rename` 原子落盘、按 `msg_id` 绑定、取"最早一条已收到"；接待"幂等 → 越权先跑 → 再抽来意 →
  汇总只生成一次 → 三种收口"；单端口 8766 按 `Upgrade` 分流 HTTP/WS、另开 **8765 视觉入站**（协议不变）。
- **Web 看板**（`master/web/`，无构建链）：状态条 / 当前到访卡 / 照片 / 历史分页 / 详情对话全文 /
  越权红条 / 断线"离线"提示；**无开门、喊话、远程控制入口**。
- **PC 先行验收** `scripts/master_sim.py`：临时库 + 临时端口起真实服务，覆盖照片乱序、
  msg_id 重放 + 重启重放、缺图、四行矩阵、复合句、否定句、告警先于照片、断网收敛；
  **证据 37/37 通过**（照片为假 JPEG 字节，真机需重跑）。
- **回归**：`py -3.12 -m pytest vision dialog master -q` → **512 passed**（476 基线 + 36 新增）。
- **本机冒烟**：`py -3.12 -m master.server`（Windows 用 `config/master.local.yaml` 覆盖 18766/18765）、
  `/api/health` 200、看板 200、`--issue-token` 可用。
- **未完成/卡点**：真机复验（用户新板址 `192.168.5.10/.11` 本机不可达：本机在 `MrZhang` 10.52.160.146，
  无 192.168.5.0/24 路由）；APK（无 JDK17/Android SDK，`dl.google.com` 403）；公网推送通道未定；
  天问 ASRPRO2.0 串口协议未定；`vision/service.py` 还没加推图调用。
- **环境坑**：Windows 8674–8773 被 Hyper-V/WSL 保留，8765/8766 绑不上（WinError 10013）；
  本机 HTTP 客户端必须 `-NoProxy`，否则被策略代理转 502。

## 第十九轮：主人端（上位机）设计定稿 + 环境基线（2026-11-11）

> 设计：[主人端-设计与接口](docs/主人端-设计与接口-2026-11-11.md)
> · 交接：[handoffs/2026-11-11-主人端设计与环境基线](docs/handoffs/2026-11-11-主人端设计与环境基线.md)
> · 依据：[安卓主人端及整机实施与验收任务书](docs/DeepSeek实施与验收任务书-安卓主人端及整机.md)

- **本轮只到设计定稿，未写实现代码**（用户明确"先不要开工"）。
- **用户决策（已确认）**：① 载体 = **服务端 + 安卓 APK 都做**；② 范围 = **主人端服务 + 键盘接待入口（任务书阶段 2–3）**，
  不做 ASR/TTS/LLM；③ 推送 = **先做可替换适配层，通道待定**（阶段 1 记"阻塞"）；
  ④ 方案 = **A 服务端先行 + 静态 Web 看板 + 最小 APK 壳**；⑤ 语音输入/播报改用**天问51**，它**只能播预置语音/短语**。
- **缺口核查（本轮最重要的发现）**：任务书要的主人端**背面服务一层在仓库里不存在** ——
  `*.py` 搜 `8766|visit_report|security_alert|set_home_state` **零实现**（只有 `dialog/privilege.py` 一句注释）；
  没有接待主循环、待发队列、照片 HTTP、`master/`、`android/`、任何页面文件。可直接复用：
  `dialog/visits.py`（10 分钟窗口 + `msg_id` 幂等）、`dialog/db.py`、`dialog/privilege.py`、`vision/ws_protocol.py`。
- **基线**：`py -3.12 -m pytest vision dialog -q` → **476 passed in 6.60s**（与 11/10 同数，代码 `ded4233`）。
- **环境基线**：本机无 Android SDK/Gradle/adb/JDK17（只有 JDK 11）；策略代理对
  `dl.google.com`/Adoptium/npm/pypi/GitHub 一律 `403`，**工具链下载不通**。
  **两块板未开机**：`192.168.43.10/.11`、`192.168.3.154` SSH banner 超时，`192.168.3.214` kex 被 reset
  （= 11/09 关机特征）。**ping 不可信**（连不存在的 `192.168.3.250` 也"能连"），判活只看 SSH kex。
- **设计要点**（正文见设计文档）：单端口 8766 按 `Upgrade` 分流 HTTP/WS；
  新增 `outbox`（`message_key` 幂等，**补传真源，不再扫 `visit.report_status`**）、
  `snapshot_asset`（照片以来源事件 `msg_id` 绑定，展示取"最早一条已收到"）、`client_device`（token 绑定）；
  消息键 `report:<visit_id>` / `alert:<visit_id>:<utterance_id>`；
  越权规则先于汇总、汇总只发一次；三种观测点（接入方确认/推送平台受理/手机展示）分别落日志。
- **未开工**：实现顺序见设计 §12（server/api → reception → outbox → snapshots+图传 → master_sim → web → android）。
- **风险**：R1 板未开机 / R2 工具链 403 / R3 推送未定 / R4 板端 Python 未知 / R5 视觉已知多报 1 条 / R6 天问51 只能预置语音。

## 第十八轮：方案 B 落地（首报延迟），真素材 6/8 → 7/8（2026-11-09）

> 说明：[首报延迟与复评](docs/首报延迟-2026-11-09.md) · 交接：[handoffs/2026-11-09-首报延迟](docs/handoffs/2026-11-09-首报延迟.md)

- **用户拍板方案 B**（延迟上报 K 帧）并已实现：`vision/service.py` 新增首报延迟 ——
  会话第一次建出来先挂起，同一轨道再撑够 **K 帧（3）或 K 毫秒（1000）先到者** 才真发；
  撑不住的按残影丢弃，它建的候选会话一起作废（`association.drop_session`）。
  配置键：`association.first_report_confirm_frames / _ms / weak_first_report_confirm_frames`。
- **复评结果**（8 条真素材，板上阈值 tau=0.45/margin=0.10/质量门 0.16）：
  **关 6/8 → 开 7/8**。修掉的是 `sc02_adjacent_t2`（原残余之一）；`sc01_replacement_t1` 仍多 1 条无身份事件。
- **关键实现细节（两处踩坑，都写进代码注释）**：
  1. 计数必须**按 track_id 各算各的**，不能"场上有多张脸就不数"——相邻双人（sc02）恰恰同场两脸，
     那样新来的人永远撑不满、直接被当残影丢掉（第一版实现就是这么错的，实测 sc02 两条 B 全不报）。
  2. 会话"出生"是弱证据（below_tau/歧义/被占）的，后面几帧即便 `track_bound`，
     强度也只有出生那一下那么强 —— 否则出画残影用 below_tau 建会话、再沿轨 3 帧就骗过确认。
     这个判据由 `association.created_reason()` 提供（会话上记 `created_reason`）。
- **残余（sc01_replacement_t1）**：慢速出画给的干净正面残影能撑 ~10 帧（比 K 帧还长），
  "活得久"这条判据分不开它和真在场 —— 只剩方案 A（重录该 take）能收口，见交档 §2。
- 本机 `pytest vision dialog` **476 项全过**；`python -m vision.run_tests` 401 项 OK；
  板端已同步 107 文件 sha256 一致、401 OK(skipped=2)、check_backend 走 TensorRT。
- `scripts/scenario_eval.py`：新增 `--defer-frames / --defer-ms` 覆盖；上报记录改以
  `_report` 为准（延迟补发也计入，否则全被记成"拦截"）。
- **收尾（2026-11-09 17:45）**：项目库提交 `83e4e46`（已推送 GitHub main）；
  知识库 aiVault 提交 `348836c`（已推送），概念页 `Edge_Vision_Association_Evidence_Gating`
  新增第 3 节「首报延迟的适用边界与两处实现陷阱」。
  **板子已关机**：`sudo` 密码为 `yahboom`（`jetson` 早前已改），`shutdown -h now` 执行后
  SSH 被 remote host 主动断开；此后 SSH 只能建 TCP、kex 阶段被重置，判定已停机。
  注意：本机网络层会给**任意** IP 伪造 ping/TCP 成功（连不存在的 192.168.3.250 也"能连"），
  以后判断板子在不在**只能看 SSH banner/kex 是否正常**，不要信 ping/端口探测。

## 第十七轮：关联层真素材回放评估 + 弱证据/质量门两处收紧（2026-11-09）

> 说明：[真实素材关联复评](docs/真实素材关联复评-2026-11-08.md) · 交接：[handoffs/2026-11-09-关联层真素材复评](docs/handoffs/2026-11-09-关联层真素材复评.md)

- **回放评估入口**（提交 `dae6499`）：`scripts/scenario_eval.py` 按 `sentinel/scenario-annotation@1`
  标注回放真素材，走的还是生产链路（onnx_pipeline → FaceTracker → `VisionService._process_frame`），
  逐帧记录关联判定（轨道/会话/理由/own/other/rms）、逐条记录上报，输出同人/异人余弦矩阵；
  自动核对标注里的 `video_sha256`；断言覆盖 replacement / adjacent / recovery / ambiguity 四类，
  `--stride` 默认 **1**（等于生产帧率）。17 项单测；`test_py36_compat` 已把它列为板端目标。
  辅助脚本上库：`scripts/make_annotations.py`（SFace 聚类草稿）、`show_annotation_timeline.py`、
  `summarize_eval.py`（矩阵表）。
- **8 条真素材**（`datasets/scenarios/real-20261108/`，gitignore）：sc01 换人×2、sc02 相邻双人×2、
  sc03 原人恢复×2、sc04 歧义×2；每条 `.mp4` + `.capture.json`（sha256/帧数/实测 fps）+ 标注 `.json`。
- **复评结果**（板上阈值 `tau=0.45 / margin=0.10 / quality_max_rms=0.16`）：修复前 5/8 通过，
  修复后 **6/8**，且两条失败从"同一人被拆成 3–4 条会话"降到"只多 1 条无身份残影"。
- **两处修复**（提交 `4eef6b9`）：
  1. `vision/association.py` 弱证据**候选挂起**：below_tau / ambiguous_margin / session_occupied
     第一次只返回 `pending_evidence`，同一轨道连续 `weak_obs=2` 帧仍弱才建会话（挂起按轨道 TTL 过期）。
     出画那一两帧侧脸不再建出重复会话，同一人的正脸不会被灌进"第二条会话"里互相比拼。
  2. `vision/service.py` 上报前**质量门** `require_quality_evidence`：一条轨道至少一帧
     `rms_norm <= quality_max_rms` 才允许发事件；实测出画/转身帧残差 0.21–0.80（甚至 25），
     本来就没资格入册，旧的"照旧上报"造成一次出画连报 3 条"新访客"。
  - 配套：`vision/config.py`、`vision/config/board.yaml` 增加两项；合同补
    「真素材复评与两处收紧（2026-11-08）」；本机 `vision 391 / dialog 75`（466）全过。
- **部署**：`pwsh -File scripts\deploy_board.ps1` → 104/104 文件 sha256 一致、`check_backend` 走
  TensorRT（yuNet/sFace fp16）、板端 `python3 -m vision.run_tests` **391 OK（skipped=2）**。
- **残余（未过严格断言）**：`sc01_replacement_t1`、`sc02_adjacent_t2` 各多 1 条无身份事件，
  来自 2–4 帧"干净正面残影"（余弦 0.29–0.38、rms 0.07–0.12），与异人分布重叠、仅凭外观不可判别。
  处置候选（重录素材 / 新会话延迟上报 K 帧 / 放宽断言）**待用户拍板**，见复评文档 §4。
- **未做**：真实门口素材（逆光/多人/快速穿过）仍未采；`below_tau` 漏关联的强口径
  "每次连续在场只 1 条事件"仍不承诺。本轮结束时板子未关机（用户要求结束对话）。

## 第十六轮：业务层 10 分钟到访窗口落地 + 板子地址修正（2026-11-08）

> 说明：[业务到访窗口](docs/业务到访窗口-2026-11-08.md) · 证据：[visit-window-2026-11-08.json](docs/data/visit-window-2026-11-08.json)

- **业务层到访窗口**（项目目标里最后一个没动的业务环节）：新增 `dialog/visits.py`
  ——`VisitBook.arrive(msg)` 把 `face_event` 归并成 `visit`，返回
  `VisitDecision(visit_id, created, reason, person_visit_count)`，`reason` ∈
  `new_visit / reuse_active / reuse_window / replay_msg`。语义照 2026-11-01 合同：固定窗口锚在
  `arrived_at`（599999ms 并入、600000ms 另建）、**活跃到访不因计时被拆**、`unk_*` 不跨 ID 互并、
  `msg_id` 幂等。新增 `visit_event` 表（`msg_id` 主键）让幂等跨进程重启有效，顺带可回答
  "这次到访由哪几条事件构成"；协议消息本身未变（v1 不加字段），`docs/02` §3.1/§6.6 已同步。
- **验收**：`dialog.run_tests` **75 项**（本轮 +28：visits 24 + 回放 4）、`vision.run_tests`
  **353 项**（+1）全过。回放证据 11 项断言全过：合成已知人脸时间线（0/5/9/10/25min + 重传）
  与 `unk_1`/`unk_2` 不互并。**真实夹具** `natural-head-full.json` 12 条事件 → **12 次到访、
  复用 0** —— 不是业务层失败，是层级边界：那 12 条是 12 个不同 `unk_*`，合同禁止业务层跨 ID 猜同人。
- **板子地址修正**：11-08 起板子在 `jetson@192.168.3.154`；`10.77.156.4` 能 ping 但 SSH 被
  reset（**已是别的设备**）。`scripts/deploy_board.ps1` 默认 `-Host` 与 `docs/部署上板`、
  `memory/02-environment.md` 已同步；板端三项自检（`/dev/video0`、`ensure_engines --check`、
  `check_backend --profile board`）全部 exit 0（TRT 8.2.1.8/fp16）。
- **可观测性**：关联层抑制日志补 `理由`（`track_bound` 分支不再只打「同类 — / 异类 —」）。
- **SFace engine 重建分支实跑通过**（上一轮只用 YuNet 在 `/tmp` 验过整条链路）：目标 engine
  不存在 → 正确回退内置 profile `min1/opt4/max8 ×112×112` → trtexec 重建 **483.0 s** →
  sidecar 自动生成且 `verify_engine` 复验通过；新 engine 与生产 engine 在 batch 1/4/8 上
  逐行余弦最低 **0.99955**、最大绝对差 **0.0156**（FP16 两次构建的正常舍入差）；生产 engine
  与 sidecar 前后 sha256 **完全未变**，`--check` 仍 exit 0。证据
  [sface-engine-rebuild-2026-11-08.json](docs/data/sface-engine-rebuild-2026-11-08.json)。
  顺带纠正一处文档事实：**板端 engine/ONNX 在部署根目录** `~/sentinel-quality-20261106/`，
  不在 `vision/models/`（那里只放原始模型）；`board.yaml` 的路径也指向根下。
- **文档**：[业务到访窗口](docs/业务到访窗口-2026-11-08.md)（语义/实现/验收/边界/复现）；
  另整理 [Jetson人脸识别从CPU到GPU-技术路线与踩坑](docs/Jetson人脸识别从CPU到GPU-技术路线与踩坑.md)
  （CPU→GPU 路线的 9 个坑与数字出处，含本次 SFace 重建耗时与证据指针）。
- **素材录制工具**（为第 3 项真实素材复评准备）：新增 [scripts/record_camera.py](scripts/record_camera.py)
  —— 预览窗口实时显示实测采集帧率，**T** 开录 / **C** 停 / **Q** 退出；停止后自动回读校验
  （帧数、分辨率、可解码），坏了直接 exit 1，并写 `<视频>.capture.json`（sha256/帧数/实测 fps，
  标标注文件直接抄）；`--list` 列出本机摄像头、设备名与 1080p/720p 实测帧率。**摄像头索引**
  （2026-11-08 实测）：`0` = 笔记本内置 HP True Vision FHD（720p≈21–26 fps），
  **`1` = 外接 HD USB Camera（接 Jetson 的那颗；720p 只有 ~9 fps，1080p 才 29.9 fps）**，
  所以默认 `--camera 1 --width 1920 --height 1080`。实测：`--headless --seconds 4`（1080p）
  → 122 帧 / 30.4 fps / 11.1 MB / 回读解码通过；交互路径用窗口消息模拟 T→C → 85 帧 / 3.0 s / exit 0。
  **踩坑**：DSHOW 与 720p 组合会退 YUY2 把外接相机压到 9 fps；OpenCV 不支持按设备名打开，
  只能索引 + `--list` 确认。+12 项单测（`vision` 353→365）。
- **--detect 实时人脸叠加 + 收口自动重写容器**（2026-11-08 晚，录制现场反馈驱动）：
  `--detect` 在预览上画人脸框/宽度/同框人数（绿色=正好 1 张脸且 ≥100 px），只画预览不录进视频；
  录制时可用 `faces=0` 当场确认"换人时画面里没人"。两个实测坑：① YuNet 的 `detect()`
  会把**整帧**设为输入尺寸，1920×1080 直接送进去 **120 ms/帧**，`--detect` 刚上线就把
  30 fps 录制拖到 **20.7 fps**；修法：检测前先缩到 640 宽（~15 ms），坐标按比例放大回原帧。
  ② 循环被拖慢时写盘头仍是 30 fps → 回放快 1.45 倍；现在收口按 `frames/elapsed` 复核，
  偏离 >10% 自动按实际帧率重写容器（原文件留 `<name>.pre-retime.mp4`，`--no-retime` 可关），
  `capture.json` 增 `capture_fps / retimed / retimed_from_fps`。已用此路径修好现场两条
  `sc01_replacement*.mp4`（20.68/21.3 fps）。+4 项单测（`vision` 367→371）。
- **真实素材首条验收数据**（2026-11-08）：`datasets/scenarios/real-20261108/` 4 条 1080p30
  （前两条因三人同框/换人重叠作废，`sc01_replacement*.mp4` 两条可用）。最后一条修帧率后
  53.4 s、4 个单人段（A→B→A→B）+4 个 `faces=0` 空档（1.9/3.9/5.1/5.6 s），人脸中位 135 px、
  87% ≥100 px。SFace 逐帧余弦：同一人 0.599–0.916（中位 0.66–0.76），不同人 **max 0.475**
  （中位 0.29–0.38）→ 现门槛 `tau=0.45` 与"不同人"上界只差 0.025，**复评时应往 ~0.50–0.55 抬**。
- **未接线**：仓库里还没有 `dialog/` 主循环，`VisitBook` 目前只在测试与回放脚本里被调用；
  离场判定（写 `departed_at`）按合同另立协议，长期未收口的到访会被一直当成活跃。
- **下次入口**：① 对话节点主循环（收 `face_event` → `book.arrive` → `created` 开迎宾 +
  `visit_report` 推送/补传）；② 离场判定合同（否则"活跃到访永久"这个限制会一直在）；
  ③ 真实门口素材到位后复评视觉层 `tau/margin/质量门`（业务窗口不参与）；
  ④ 换板/重刷后先 `python3 scripts/ensure_engines.py --check`（**YuNet 重建约 150 s、
  SFace 约 483 s**，别以为卡死）；⑤ 板端 U 盘根分区开机报 EXT4 `ext4_mb_generate_buddy`
  不一致（group 324/328/329）——**有文件系统损坏风险**，重要证据先备份，别只留在这块盘上。

## 第十五轮：两个 P0 修复 + 现场能力三件套，板端全同步（2026-11-07 晚）

> 交接：[docs/handoffs/2026-11-07-摄像头打通与可靠性修复.md](docs/handoffs/2026-11-07-摄像头打通与可靠性修复.md)
> 叙事：[docs/P2摄像头打通与公开素材复评-2026-11-07.md](docs/P2摄像头打通与公开素材复评-2026-11-07.md)

- **帧级均匀保护**（`detection.blank_std=2.0`）：近黑/被遮帧在检测前被拦掉。板端实测：全黑 10 帧 → 0 事件（修复前是 1 条假陌生人）；黑/真/黑混排 30 帧 → 空白 10 + 事件 1（真帧已知人脸 conf 0.89）；真实素材帧 std 最低 **17.66**、中位 37.7~46.1，阈值 2.0 有 8.8 倍余量。
- **WS 出口有界**（`server.timeout=1.0 / send_timeout=0.5 / max_reconnect_sec=60`，退避 3→6→12…s）：同一段素材 12 s 三跑 —— 无出口 14.26 fps、端口拒绝 14.40（1.01×）、**半死对端（accept 后不应答）13.11（0.92×，最大帧间隔 1070 ms = 一次握手超时，之后退避不再重复探测）**。原来对端"半死"会把主循环永久挂死。
- **现场能力**：`scripts/camera_watch.py`（SSH 纯终端每秒一行，板上实跑 fps 18.4~19.8 / 人脸 0 空房间）、`scripts/enroll_faces.py --auto`（无头采集+质量门逐张打印，本机素材 3/3 采纳、板上空房间走到"未采纳" exit 1 且相机正常释放）。
- **部署/engine**：`scripts/deploy_board.ps1`（tar+scp → 板端 tar 哈希 → 解包 → sha256 清单 → check_backend → run_tests；**99 文件 / 35.25 MB / sha256 99-99 一致 / 352 项 OK**，幂等）+ `scripts/ensure_engines.py`（`--check` exit 0，0.3 s；`/tmp` 副本真重建 150.6 s，生产 engine 哈希不变）。
- 测试 **352 项**（本机 + 板端全过，板端 2 skipped）；`test_py36_compat` 的 BOARD_TARGETS 已补 `camera_watch.py`/`ensure_engines.py`。
- 文档：[P2摄像头打通与公开素材复评](docs/P2摄像头打通与公开素材复评-2026-11-07.md)（第一~六节）、[无头注册与终端自检](docs/无头注册与终端自检.md)、[部署上板与engine校验](docs/部署上板与engine校验.md)；证据 [reliability-fixes-2026-11-07.json](docs/data/reliability-fixes-2026-11-07.json)。
- **下次入口**：① 真实门口素材（用户之后给）→ 重标定 tau/margin/质量门；② 真人端到端时延（人站镜头前 10 s，配合 `camera_watch --jsonl`）；③ 已知人脸 10 分钟到访窗口（业务层，未动）；④ 关联层抑制日志在 `track_bound` 路径把 own/other 打"—"；⑤ SFace engine 重建分支未实跑；⑥ 换板/升级 JetPack 后先 `python3 scripts/ensure_engines.py --check`。

## P2 摄像头打通 + 公开素材复评：1280×720 6.2→17.8 fps，关联层三判据全过（2026-11-07 第十四轮）

- 摄像头接上了（`/dev/video0` UVC，`ssh jetson@10.77.156.4`，昨天记的 `192.168.3.154` 已不通）。生产配置 `backend: auto` + 不设 FOURCC 会被 V4L2 协商成 YUYV（USB2 采集 9.95 fps，管线 **6.2 fps**）；改 `backend: v4l2` + `fourcc: MJPG` + `fps: 30` 后 **17.6–17.8 fps**（20 s ×2，推理 26.0 ms，丢帧 0），`check_backend.py --profile board` 退出码 0。
- 代码：`vision/config.py` 加 `camera.fourcc/fps`（默认 `""`/`0` = 不干预）；`vision/camera.py` 打开顺序改为 FOURCC→FPS→分辨率→BUFFERSIZE，并新增「预热帧全失败就抛 `SourceError` 并提示改 v4l2」（`auto` 在该 L4T OpenCV 4.1.1 会落到 GStreamer 后端，设 MJPG 后连读 50 帧失败）。单测 319→**330**（本机与板端全过，板端 2 skipped）。说明/命令见 [P2摄像头打通与公开素材复评](docs/P2摄像头打通与公开素材复评-2026-11-07.md)。
- 素材：真实门口素材仍采不到，改用 8 条 Coverr 免费片（1920×1080 H.264，`datasets/doorway-20261107/`，gitignored，带 manifest/LICENSE/预览帧）。板上生产路径复评一致：同一人出画再回 **1 条**（抑制 1）、不同人先后 **2 条**（0 合并）、两人并排 **2 条**；注册 A 后换姿态再认 **is_known conf 0.849**，B 素材 **conf 0.114** 未错认。证据 [material-association-2026-11-07.json](docs/data/material-association-2026-11-07.json)。
- **新坑（当晚已修，见上节）**：全黑帧会让 YuNet(2022, `yunet_trt320.onnx`) 输出 score **0.8862**、bbox 比画面还大的假脸（`overlap` 分支输出 1.0688，被 decode clip 成 1）；暗帧(16) 也过 0.6 门限 → 摄像头被遮/夜间无红外/流掉线会误报陌生人。CPU dnn 与 TRT 结果一致，不是 engine 问题。证据 [detector-black-frame-fp-2026-11-07.json](docs/data/detector-black-frame-fp-2026-11-07.json)。
- 下次入口：**先定近黑帧策略**（建议帧级均匀性保护 + 检测框合理性兜底），再修 WS 发送失败阻塞主循环；现场注册仍缺无头路径（`enroll_faces.py --camera` 依赖 `cv2.imshow`）；真实门口素材、真人端到端时延仍未验。板端工作目录不是 git，本轮已手动同步 `vision/`（含新 `vision/tests/`）与 6 个 scripts，**没有版本校验**。

## P2.5 关联层接线完成：自然姿态 12 → 3 条，出画再回 5 → 3 条（2026-11-07 第十三轮）

- 决定一 **B**（本轮接关联层）、决定二 **D**（`auto` + 可观测）已落地：`VisionService` 每帧把 `embedding`+`rms_norm` 喂给 `AnonymousAssociator`，tracker 回填 `track_id`，出事件时按会话去重（已报过就抑制，计入 `stats.suppressed`）；已知人脸不变；v1 消息协议未动。`board.yaml` 打开关联层（`enabled: true`），`base/sim` 仍是关。
- **四个板上量出来的修正**：① 质量门按 320 输入重标定 `0.10 → 0.16`（入册率 2.5%→58%，异类余弦上界仍 0.209 ≪ tau 0.45）；② `observe(..., tracked_ids=)` 让换轨能交接（否则框抖动把同一人复制成两条会话，own 0.888/other 0.856 → 判歧义 → 事件反弹）；③ 修 `track_bound` 不刷新 `last_ts` 的 bug（连续站 120 s 会把会话判过期、重报一条）；④ 同帧重复检出（幽灵框 IoU>0.5）先去重。
- **验收**（板端，TRT）：287 点夹具（几何来自夹具 + 真实外观证据，463/463 框匹配）关联层关 **12** 条 → 开 **3** 条（抑制 9），剩 1 条是 `below_tau` own 0.38 的保守重报；生产几何整段 143 s 视频：稳定在场 3→3、挖 3 s 空档 **5→3（抑制 2）**。开销 **+1.23 ms/帧**（56.3 ms 帧的 2.2%）；`--no-server` 下开关关联层 15.8/16.0 fps。
- **决定二 D**：`scripts/check_backend.py`（`--expect tensorrt` 退出码 0，`--expect dnn` 退出码 1）+ 心跳新增 `backend: "yunet-onnx/tensorrt"` 字段，把 `auto` 的静默降级变成可断言、可观测；单测 `test_check_backend.py` 钉住退出码。
- 单测 **319 项全过**（+36）。顺带发现：**WS 连不上时发送失败会阻塞主循环**（同一段视频 8.2 vs 16.5 fps），P2 联调要盯。
- 下次入口：P2 板端全量回归（**要先插 USB 摄像头**：实时 fps/端到端时延、真机注册再认、真实门口素材复评 `tau`/质量门），步骤见 [P2.5完成与P2入口](docs/handoffs/2026-11-07-P2.5完成与P2入口.md)；说明 [P2.5-关联层接线](docs/P2.5-关联层接线-2026-11-07.md)，证据 `docs/data/{quality-gate,association-replay,association-service,association-cost}-2026-11-07.json`。

## P1 裸 ONNX 管线接线完成：板上 3.69 → 17.65 fps（2026-11-07 第十二轮）

- 新增 `vision/onnx_pipeline.py`：`DnnRunner`/`TrtRunner`、`OnnxYunetDetector`（320→原图缩放、留 `raw_row`）、`OnnxSfaceRecognizer.embed_batch`、`create_pipeline`（逐组件回退+预热）、`create_components`（`legacy/auto/cpu/trt` 统一入口）；配 `vision/align.py` 五点对齐（S1）。
- `vision/service.py` 改成一帧一次批量识别（`_embeddings`/`_match`，批量条数不符退逐张）；配置新增 `runtime.backend / detector_onnx / detector_engine / recognizer_engine / recognizer_batch / warmup`；`board.yaml` 切 `auto` + 板上绝对路径；`vision_sim` 加 `--backend`。
- **板端验收**：与旧路径 parity —— 69 vs 69 脸 / 0 帧数量不一致 / 解码行最大差 **0.0** / 特征余弦 **min 1.000000**；CPU vs TRT（0.6 阈值）79 vs 79 / bbox 0.0001 / 余弦 0.999978。整链真 `VisionService`：**CPU 3.69 fps（271 ms）→ TRT 17.65 fps（56 ms，4.8×）**，事件条数与 bbox 完全一致；`--profile board` 真机入口 12 s 209 帧 17.3 fps。
- **抓到真坑**：OpenCV 4.1.1 批量 dnn 会把同一批的脸特征塌成同一个向量，且状态粘住（批量 8 时 8 行两两余弦=1，之后单张也回不去）。处置：`DnnRunner.max_batch = 1`，`recognizer_batch` 只对 TRT 生效，被钳时打日志，单测钉死。
- 单测 **283 项全过**（+31）。默认仍 `legacy`，只有 `board.yaml` 是 `auto`；关联层仍未接线，自然姿态 12 条事件未解决；板上仍无 `/dev/video*`。
- 下次入口：P2 板端全量回归（插 USB 摄像头 → 实时 fps/端到端时延 → 真机注册再认 → 复现关联层问题），命令与风险见 [P1完成与P2入口](docs/handoffs/2026-11-07-P1完成与P2入口.md)；说明见 [P1-ONNX管线接线](docs/P1-ONNX管线接线-2026-11-07.md)。

## P0 解码先验预计算完成，下一步 P1 接 VisionService（2026-11-07 第十一轮）

- 新增 `vision/yunet_decode.py`：四档先验表（5875 条）模块加载时算一次缓存，热路径只剩「按分筛 → 解偏移 → NMS」；公式顺序与旧 `decode()` 逐字节一致，Python 3.6 可跑。
- **等价性**（固定视频 40 帧，哈希一致）：PC 与板端都是 **69 脸不变 / 0 帧数量不一致 / 40/40 帧逐位完全相同 / bbox 最大差 0.0 px**。
- **速度**（板端 cv2 4.1.1 / numpy 1.19.5）：解码中位 **32.689 ms → 0.799 ms（41×）**，目标 <5 ms 有 6 倍余量；PC 是 5.796 → 0.217 ms。检测段端到端（engine + 新解码）**45.87 → 22.53 ms（2.0×）**，CPU 路径 83.8 → 49.15 ms。
- 本机单测 **239 项全过**（原 228 + 新增 11），`py -3.12 -m vision.run_tests` 1.26 s。
- 边界：只做解码，**没有接 `VisionService`**，生产默认仍是 CPU/Haar、`trt` 未启用；SFace/对齐/事件链/关联层都没动。
- 下次入口：P1 接线（`runtime.backend: auto|cpu|trt` + 预热 + 回退 + 单测），完整步骤与命令见[解码预计算与 P1 入口](docs/handoffs/2026-11-07-解码预计算与P1入口.md)。说明见 [解码先验预计算](docs/解码先验预计算-2026-11-07.md)，证据 `docs/data/decode-parity-2026-11-07.json`。
- 运维提醒：板子现在在 **`192.168.3.154`**（昨天记的 `10.77.156.4` 已连不上），免密登录照旧。

## 板端腾空间完成，明天从 P0 开始（2026-11-06 第十轮）

- 按要求本轮不动 P0–P3，只做板端盘点 + 清理 + 排期。**Jetson `/dev/sda1`：45G→22G 已用，空闲 7.1G→31G（87%→42%）**；`/home/jetson` 19G→1.1G。
- 删掉：`jetson-inference` 3.8G、`librealsense` 1.8G、`opencv`/`opencv-3.4` 源码 1.6G、亚博教程 `software`/`yahboomcar_ws`/`catkin_ws`、torch+tensorflow 全家（含 `.local` 里 933M+677M）、DeepStream 包、darknet、pip 缓存、`.ros/log`、`ROS专用配件(源码).zip` 1.2G、零散 tarball；Docker 里 3 年前的 `yahboomtechnology/ros2-base` 镜像 5.61G 已删除。
- 保留：`/usr` 系统栈（cv2 4.1.1 / TensorRT 8.2.1.8 / cuDNN / CUDA）、`~/.local` 的 pycuda+numpy（板上 import 复验通过）、`~/sentinel-quality-20261106` 116M、两个 `sentinel-check-*`（模型+测试视频）、`/var/swapfile` 4G。
- 未清（需 sudo，板上无免密）：`/var/cuda-repo-l4t-10-2-local` 1.1G、`opencv_install` 46M 与 `trt_pose` 43M 的 root 属主残留、`~/.nv/ComputeCache`、`/media/jetson/...` 226M。
- 排期估时：P0 1–2h、P1 4–6h、P2 1–2h；**P3 被硬件卡住——板上没有 `/dev/video*`（lsusb 只有蓝牙），要先插 USB 摄像头**。合计顺的话一个整工作日到 P2。
- 明天入口：**P0 解码先验预计算 → 做成 `vision/yunet_decode.py`**（先验表缓存，目标 <5 ms，40 帧 parity ≤0.0001 px），验收与命令见[板端存储清理与明日入口](docs/handoffs/2026-11-06-板端存储清理与明日入口.md)。

## 本次会话收尾：下个对话继续人脸识别提速（2026-11-06 第九轮）

- 本会话完成：板端质量门复验、合成场景关联验收、TensorRT 只读评估、TensorRT 封装（`vision/trt_backend.py`，默认关闭，228 项单测）。
- 速度基线（板端 40 帧中位）：检测段 83.8 ms（engine 11.7 + 解码 31）；SFace 218.1 ms → TRT 批量 35.8 ms。
- **下次入口**：先做解码先验预计算（`decode()` 每帧重建 5875 先验约 31 ms，目标 <5 ms，检出/bbox 逐帧不变）；再接 `VisionService`（`runtime.backend: auto`，失败回退 CPU）；然后板端全量回归 + 摄像头实时延迟。
- 完整计划、命令与验收指标见[人脸识别提速交接](docs/handoffs/2026-11-06-人脸识别提速交接.md)。
- 边界不变：默认 CPU，TRT 不启用；真实门口素材、歧义场景、关联层接线、自然姿态 12 条事件均未解决。

## TensorRT 封装完成（2026-11-06 第八轮）

- 新增 `vision/trt_backend.py`：sidecar 哈希绑定（engine/模型 sha256 + TensorRT 版本 + 输入 profile）、动态批量（SFace 1/4/8）、无 TensorRT 时 `create_backend()` 返回 `None` 回退 CPU；17 项单测。
- 板端回归（40 采样帧）：检出 69 vs 69、bbox 最大差 0.0001 px、69 张脸特征余弦最小 0.999979；检测段 83.8→45.9 ms（1.8×）、SFace 218.1→35.8 ms（6.1×）。
- 关键发现：检测只快 1.8× 是因为旧 `decode()` 每帧纯 Python 重建 5875 个先验框 + NMS ≈31 ms；单独 engine 11.7 ms。接生产前先把解码预计算/numpy 化。
- 默认仍不启用，未接 `VisionService`。证据 `docs/data/trt-wrapper-regression-2026-11-06.json`，说明 [TensorRT 封装](docs/TensorRT封装-2026-11-06.md)。

## Jetson TensorRT 加速评估（2026-11-06 第七轮）

- 板端 `cv2` 4.1.1 未编 CUDA（`getCudaEnabledDeviceCount()=0`），检测/识别一直走 CPU；CUDA 10.2 与 TensorRT 8.2.1 实际都在板子上。
- 只读基准（40 帧采样）：CPU 中位 检测 84.0 ms + SFace 219.0 ms/帧 ≈ 309 ms/采样帧；TRT FP16 检测 11.2 ms + SFace 18.1 ms/脸 ≈ **35 ms/帧（单脸，约 8.8×）**。
- 正确性：YuNet 最大绝对差 7.5e-6；SFace FP32 余弦 1.0000、FP16 0.99999（最大绝对差 0.0113）。
- 两个坑已定位：YuNet ONNX 声明 120×160 但实际喂 320（需改声明维度的实验副本）；pycuda 只存 int 指针会被 GC 导致 `invalid argument`（须保留 `DeviceAllocation`）。
- 未接生产：Engine 封装、FP16 回归、回退 CPU、engine/模型哈希绑定都还没做。证据 `docs/data/trt-acceleration-2026-11-06.json`，叙述 [Jetson TRT 加速评估](docs/Jetson-TRT加速评估-2026-11-06.md)。

## 合成场景关联验收通过（2026-11-06 第六轮）

- 现场不便拍摄，改用公开 LFW 同人/异人配对构造三段受控片段（替换 / 相邻 / 恢复），跑 旧版检测 → SFace → `FaceTracker` → `AnonymousAssociator`（τ=0.45、δ=0.10、质量门 0.10）。
- 三段全部通过：替换场景 2 条轨道 2 个会话、第二人 `own_best=−0.0556` 未继承；相邻场景同帧两条轨道各建会话、不互占；恢复场景第二段 `own_best=0.9851` 关联回原会话（2 轨道 1 会话）。
- 过程中发现旧版解码器大脸先验只有 5×5 网格（步长 64）：合成片段把脸放在 x=190 会整帧漏检，移到 x=150 稳定检出——固定机位素材需先试位置。
- 边界：合成静帧拼接，不是门口视频；没有运动模糊/遮挡/光照；只覆盖一个身份对，歧义场景未做。真实素材仍要补。
- 证据：`docs/data/scenario-association-eval-2026-11-06.json`、[合成场景关联验收](docs/合成场景关联验收-2026-11-06.md)、[交接](docs/handoffs/2026-11-06-场景关联与板端复验.md)。

## 板端复验完成：质量门跨 OpenCV 版本成立（2026-11-06 第五轮）

- 用同一份哈希固定的视频/模型/辅助脚本在 Jetson（Python 3.6.9 / OpenCV 4.1.1）重跑质量研究。板端需 `export OPENBLAS_CORETYPE=ARMV8`，否则 numpy/cv2 导入 `Illegal instruction`；已装本机 SSH 公钥，`jetson@10.77.156.4` 可免密登录。
- 结果与 PC 一致：`rms_norm≤0.10` 两端都只保留 25 张脸，且同侧最低 > 异侧最高（PC 0.624/0.2046，板端 0.5724/0.2239，仅余量收窄）；参考库仍是 16 左 + 5 右；τ=0.45/δ=0.10 覆盖 97.8%（PC 98.4%），歧义 0。
- 板端多出 1 张"本方低于异方"的脸（margin −0.043），恰被 δ=0.10 边距规则拒绝，不会误关联：趋势验证了边距规则的必要性。
- 边界不变：两端都是同一条视频的时间切分，只证明指标可移植，不证明换人/相邻/恢复/歧义场景能力。
- 证据：`docs/data/quality-board-rerun-2026-11-06.json`、[质量筛选与保守参考策略](docs/质量筛选与保守参考策略-2026-11-06.md)。下一步仍是拍 4 段独立场景素材 → 过 τ=0.45 复算 → 再接 `VisionService`。

## 短期匿名关联层核心已实现（2026-11-06 第四轮）

- 新增 `vision/association.py`：只按"证据唯一"复用会话——`own>=τ=0.45`、`own−other>=δ=0.10`、候选未被其他活跃轨道占用；否则新建独立候选并返回理由。入册只收 `rms_norm<=0.10` 的高质量脸；会话 120s 过期、每会话 8 条参考、最多 64 会话，内存驻留不含图像。
- 新增 20 项单测（`vision/tests/test_association.py`），含同人跨轨恢复、异人不合并、相邻双人不抢会话、歧义保留独立候选、低质量不入册、TTL/容量/reset；本机全量 **211 项通过**（原 191 项不变）。
- 配置新增 `association.enabled=false`（默认关闭）与阈值键，`config.validate` 校验非法值；`base.yaml` 只写可见阈值。**未接入生产主循环**：embedding/质量指标尚未接到 `VisionService`，tracker/事件协议未改，session_id 未进 v1 消息。
- 下一步：独立场景素材验收（换人/相邻/恢复/歧义）→ 通过后接 `VisionService` 并跑生产事件链路 → 板端复验。证据见[关联合同](docs/跨轨关联与两层去重合同-2026-11-01.md)第"短期匿名关联层实现"节。

## LFW 独立配对验证与 τ 修订（2026-11-06 第三轮）

- 网上找到独立带标签素材：Hugging Face `datasets-server` 的 `logasja/lfw` `pairs/test`，取 label 1 前 500 对（同人）+ label 0 前 500 对（异人）。LFW 原图 250×250 只存 `datasets/lfw-pairs/`（忽略目录），证据文件只保留余弦/质量汇总，不含图像或特征向量。
- 标签语义用数值确认（同人中位 0.667 vs 异人 0.090）。同一隔离路径（YuNet opset9→五点对齐→SFace）1000/1000 对全检出：AUC 0.9939、EER 1.6%（阈值 0.289）；异人最高余弦 0.353，τ=0.36/0.45/0.50/0.60 时 FAR 均为 0/500，与公开参考阈值 0.363 一致。
- 质量门（`rms_norm≤0.10`）在 LFW 上几乎无增益（AUC 0.9944 vs 0.9939）：LFW 是正脸高质量图，质量门解决的是视频域低质量/侧脸。
- 修订候选操作点：**τ=0.45、δ=0.10**（0.50 为保守备选）。依据：LFW 500 个异人对 FAR=0；视频关联覆盖 95.6%→98.4%，错关联仍 0。上一版 τ=0.60 保留为回退值。仍是待场景验收的候选，不是冻结值。
- 边界不变：视频部分仍只有一条视频、两个身份、时间切分非独立；LFW 只能校准图像域阈值，换人/相邻/恢复/歧义仍要拍素材。生产关联未启用、整体可靠性仍未通过。
- 证据：[质量筛选与保守参考策略](docs/质量筛选与保守参考策略-2026-11-06.md)、[LFW 结构化数据](docs/data/lfw-verification-eval-2026-11-06.json)。下一步：按[素材协议](docs/独立正负例素材采集与标注协议-2026-11-06.md)拍 4 段素材，用 τ=0.45 复算 → 板端复验。

## 质量筛选与保守参考策略已评估（2026-11-06 第二轮）

- 单一全局阈值确认不可用：494 张检出脸、260 个有检出采样，同侧最低余弦 0.0029 vs 异侧最高 0.3869；最差同侧对来自 21.5s 近侧脸与 61.5s 低质量脸。
- 常见质量门都救不回来：score≥0.99 时同侧最低仅 0.1271（异侧最高 0.3489）；人脸尺寸全部达标、无效；yaw≤0.22 仍重叠。**对齐残差 `rms_norm`（五点拟合残差÷眼距）≤0.10 能分开两个身份（同侧最低 0.624 > 异侧最高 0.205），但只保留 25/494 张脸（5%）**。
- 质量门控入册 + 保守参考库（前 50s 用 `rms_norm≤0.10` 入册 16 左 + 5 右，之后每张脸比对参考）：后 315 张脸中本方相似度 - 异方相似度**全部为正（0 错关联）**；τ=0.60/δ=0.10 时关联覆盖 95.6%，低质量脸覆盖 94.9%。τ∈[0.60,0.70]、δ=0.10 为待校准候选，不是冻结值。
- 边界：同一视频时间切分、非独立素材；仅 2 人固定构图、仅本机 PC；未覆盖换人/相邻双人/恢复/歧义；板端关键点质量未复验。**生产关联仍未启用、整体可靠性仍未通过**。
- 下一步：按[素材协议](docs/独立正负例素材采集与标注协议-2026-11-06.md)拍 4 段独立素材并标注 → 用同一指标复算 τ/δ → 板端复验质量分布 → 通过后才写关联层实现。证据见[质量筛选研究](docs/质量筛选与保守参考策略-2026-11-06.md)与[结构化数据](docs/data/quality-reference-study-2026-11-06.json)。

## 完整视频隔离推理评估完成（2026-11-06 第一轮）

- 本机与Jetson均顺序解码1719帧、完成287个固定采样点的旧YuNet实验副本检测、五点对齐及SFace特征。两端视频、模型及辅助脚本哈希一致；未升级依赖、改生产配置或保存新图像/特征向量。
- Jetson OpenCV4.1.1：左侧245/287、右侧248/287点有检出，两侧同时234点，28点无检出；检出候选493份有效128维特征，处理窗口83.163秒。
- 板端同侧配对最低余弦0.04036，异侧最高0.39788；分布有重叠，不能直接冻结全局身份阈值。左右仅为此固定构图的评估标签，配对不是独立样本；没有逐脸框真值，不报precision/recall。
- 本机191项回归通过。此次证明全片隔离执行能力，不代表生产关联或业务去重通过；原自然姿态12条本地事件的失败保留。
- 下一步补独立的同位置换人、跨位置相邻双人、原人恢复与歧义用例，评估质量筛选/保守参考策略后再定适配器与门槛。完整记录见[本轮评估](docs/完整视频隔离评估-2026-11-06.md)，交接见[2026-11-06交接](docs/handoffs/2026-11-06-完整视频隔离评估.md)。

## 旧版YuNet实验链路已跑通（2026-11-01）

- 原YuNet2022在板端同样导入失败。对固定模型的3个常量Slice和5个默认MaxPool属性生成实验opset9副本，ONNX checker通过；PC四帧与原模型全部输出数值差0。
- Jetson原有OpenCV4.1.1已执行实验副本检测、五点解码、对齐和SFace特征；四个视频帧分别检出2、0、0、1脸，检出脸均有128维有限特征。未升级系统或修改生产配置。
- 仅证明四帧隔离路径可执行；姿态覆盖与人员关联质量未验收，原自然姿态及整体可靠性仍未通过。
- 下一步对完整287点视频评估覆盖与特征正负分布，并补换人/相邻双人/歧义用例，再冻结适配器与关联门槛。详见[隔离链路验收](docs/旧版YuNet隔离链路验收-2026-11-01.md)。

## 模型兼容性实测（2026-11-01，本轮新增）

- 已下载两个OpenCV官方模型并核对两端哈希。本机真实检测/对齐/SFace推理得到2脸、各128维有效特征。
- Jetson现有OpenCV4.1.1导入YuNet 2023失败；SFace可加载并对未经对齐的公开视频整帧缩放张量执行前向，输出1×128且有限。后者只证明计算图可执行，不代表人员识别通过。
- 未改生产算法、配置或系统依赖；自然姿态与整体可靠性仍未通过。下一步验证兼容的检测/五点关键点路径，再做对齐与身份区分验收。
- 详情：[模型兼容性实测](docs/模型兼容性实测-2026-11-01.md)。本轮不再停留于“没有模型”的判断。

## 本轮续接：保守关联已确认，板端能力核查完成（2026-11-01）

- 已核查tracker/service/recognizer与dialog实际实现：当前Haar+none未提供跨轨身份特征，dialog尚无状态机或业务visit_report去重实现。
- 本轮本机191项测试通过（无失败/错误/跳过）；287点夹具重新得到12条事件（左7右5），完整事件时刻与原记录一致。另给出“原人恢复/同位置换人”具有完全相同框与时间输入的反例，说明仅坐标不能可靠区分两者。
- 已写[跨轨关联与两层去重合同](docs/跨轨关联与两层去重合同-2026-11-01.md)及[结构化复核](docs/data/jetson-2026-11-01-association-contract-baseline.json)。用户已确认：证据充分且唯一时才关联，歧义保留独立候选，先验证能力。
- [板端能力核查](docs/短期关联能力验证-2026-11-01.md)已完成：Jetson为Python3.6.9/OpenCV4.1.1，项目所用FaceDetectorYN/FaceRecognizerSF API缺失，两个隔离目录无vision/models；系统公共目录有Haar/LBP级联。dnn/ORB/光流API存在但未验证人员区分能力；不能仅拷模型就认为现有识别路径可用。
- 本轮仅只读连接板端；没有改生产算法/配置、升级系统、下载模型、读取身份库或采现场图像。模型/API前置条件未满足，关联推理未测，原自然姿态及整体可靠性仍未通过。
- 下一步准备并验证模型与兼容推理后端组合：先调查现有dnn的实际模型兼容性，必要时比较隔离运行时；明确新范围后做有限推理与正负例校准，再委派关联实现。不能直接延长TTL或以事件减少宣告修复。

## 最新状态：600秒压力通过，自然姿态本地去重未通过（2026-11-01）

- **本轮范围**：新增独立循环压力工具及40项测试，没有修改生产算法或阈值。本机和Jetson完整191项测试均通过；独立审查发现的3个工具问题已修正并复审。
- **资源验收通过**：真实进程600.169秒，外部驱动601.250秒；7201解码帧、1201采样、4圈完整原片；无处理/协议错误。预热60秒后RSS下降3.742MiB，FD和线程增长均为0；115份资源快照与源码哈希已独立核对。
- **自然姿态本地目标未通过**：完整287个2Hz采样点标注两名人员持续在场，预期2条face_event，实际12条（左7右5，来自12个不同轨道）。287点坐标夹具可无视频复现。
- **层级边界**：这是较现有TTL轨道合同更严格的本地事件目标；最终visit_report及其10分钟业务去重未运行，不能写成12人或12次到访。物体遮挡、自然出画重入、识别及多日稳定性仍未验收。
- **下一步**：先冻结跨轨身份/到访关联与两层去重责任，保留自然姿态失败夹具、相邻双人和前轮三个正例，再限定实现；不要仅延长TTL或减少事件数。**整体可靠性仍未通过。**
- 当前入口：[自然姿态本地事件验收](docs/自然姿态本地事件验收-2026-11-01.md)、[600秒稳定性验收](docs/板端稳定性验收-2026-11-01.md)、[任务合同](docs/自然场景与稳定性任务卡-2026-11-01.md)；汇总见docs/data同日期natural-presence与stability两份acceptance.json。
- 压力隔离目录：`/home/jetson/sentinel-check-20261101-stability-01`；自然原片运行在原`sentinel-check-20261101-consecutive-170927`目录。未替换生产部署、调用摄像头、读取人脸库或保存现场图像。
- 原始压力证据在`logs/video-validation/stability-board/`，自然结果在`logs/video-validation/natural-head-full.json`；均忽略。进程环境仍需`OPENBLAS_CORETYPE=ARMV8 OPENBLAS_NUM_THREADS=1 OMP_NUM_THREADS=1`，系统包未改。
- 前轮连续确认修复及三个冻结视频正例已通过，见[原验收](docs/连续确认修复验收-2026-11-01.md)；本轮没有重复改动该算法。
- Git：按用户要求每轮结束前检查实际范围、提交并推送GitHub；模型、媒体、认证和运行产物排除。

## 正式收尾与继续入口

本轮完整交接：[完整视频隔离评估交接](docs/handoffs/2026-11-06-完整视频隔离评估.md)（2026-11-06，最新）。
上次交接：[连续确认与稳定性验收收尾](memory/session-logs/2026-11-01-Jetson连续确认与稳定性验收收尾.md)。

下一次先读本页与最新交接，再看自然姿态失败夹具和两份最新验收报告。先补独立正负例（换人/相邻双人/原人恢复/歧义），评估质量筛选与保守参考策略，再冻结适配器与门槛；不要仅延长TTL或减少事件数。

修复前历史保留在[前次交接](memory/session-logs/2026-11-01-Jetson可靠性修复与公开视频回归.md)及对应docs验收文件；历史2过1败不再作为当前连续确认结果。

