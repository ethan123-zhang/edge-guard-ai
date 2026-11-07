# 部署上板与 engine 校验

> 板端：`jetson@10.77.156.4:~/sentinel-quality-20261106`（**不是 git 仓库**，靠手动同步）
> 环境：JetPack 4.6 / Python 3.6.9 / OpenCV 4.1.1 / **TensorRT 8.2.1.8**
> 两个脚本：`scripts/ensure_engines.py`（板端跑）、`scripts/deploy_board.ps1`（Windows 跑）
> 实测日期：2026-11-07

## 1. 为什么要校验 engine

engine 旁边必须有 `<engine>.json`（sidecar，schema `sentinel/trt-engine@1`），里面绑**三样**东西
（`vision/trt_backend.py:verify_engine`）：

| 绑定项 | 什么时候会漂 |
|---|---|
| engine 文件 sha256 | 手拷了别的 engine、文件传坏、被截断 |
| 源模型 sha256 | 换了 ONNX（哪怕只差几个字节）、重新导出 |
| 建 engine 时的 TensorRT 版本 | **换板 / 重刷 JetPack / OTA 升级** → 8.2.1.8 变别的 |

`runtime.backend: auto` 的取舍是「engine 不可用就退 CPU dnn」：**服务不崩，只是从 17.6 fps 掉到
3.7 fps（约 8.8×）**，唯一线索是日志里一行 `[vision] TensorRT 不可用，回退 CPU：…`。
所以校验必须前移成**部署闸门**（`scripts/check_backend.py --profile board` 不通过 = 部署失败），
而不是等现场觉得卡了才发现。

**`verify_engine()` 管不到的一类漂移是形状**：YuNet 那份 ONNX 里声明的输入是 `1×3×120×160`
（历史遗留），而管线一直喂 320×320；照 ONNX 声明建出来的 engine 一喂 320 就维度越界。
SFace 是 112×112 动态 batch，上限 8（`runtime.recognizer_batch` 也钳在 8）。
`ensure_engines.py` 除了调 `verify_engine()`，还按这两条约定额外核对 sidecar 里的 profile。

## 2. 脚本一：`scripts/ensure_engines.py`（板端，py3.6 兼容）

流程固定为**校验 → 缺了/漂了重建 → 复验**，每个 engine 打印一行结论。

```bash
# 板端（照例先补线程环境；脚本在 ARM 上也会自己补齐这三项）
export OPENBLAS_CORETYPE=ARMV8 OPENBLAS_NUM_THREADS=1 OMP_NUM_THREADS=1
cd ~/sentinel-quality-20261106

python3 scripts/ensure_engines.py --check        # 只体检；需要重建 exit 1
python3 scripts/ensure_engines.py                # 缺的/漂的重建，好的不动
python3 scripts/ensure_engines.py --force        # 换板后第一次：无条件重建
python3 scripts/ensure_engines.py --fp32 --force # 换精度
```

| 参数 | 作用 |
|---|---|
| `--profile board` | 配置档（默认 board），从 `vision/config/board.yaml` 读 `runtime.detector_engine` / `runtime.recognizer_engine` |
| `--check` | 只校验不重建；需要重建时 exit 1（与 `--force` 互斥） |
| `--force` | 不看现有 engine，无条件重建 |
| `--fp16` / `--fp32` | 精度，默认 fp16 |
| `--engine PATH --model PATH` | 只处理指定的一个（**在 /tmp 试验必须用这个，别动生产 engine**） |
| `--trtexec PATH` | 指定 trtexec；默认先找 `/usr/src/tensorrt/bin/trtexec`，再找 PATH |

退出码：`0` = 全部可用；`1` = 有 engine 不可用 / `--check` 下需要重建 / 重建失败；`2` = 配置读不了。

**重建细节**（参数不是猜的，来自板上历史脚本 `trt_regression.py` / `trt_bench.py`）：

- 输入名与 profile **先读现有 sidecar 的 `input` 字段**；sidecar 丢了才用内置约定值并在输出里打
  `[warn]`。YuNet 固定 `1x3x320x320`，SFace `min=1 / opt=4 / max=8` × `3x112x112`。
- **静态 profile 不传** `--minShapes/--optShapes/--maxShapes`（YuNet 当年就是这么建的）；
  只有 sidecar 的 min≠max（动态 batch）才传三个形状：
  `--minShapes=data:1x3x112x112 --optShapes=data:4x3x112x112 --maxShapes=data:8x3x112x112`。
- 先建到 `<engine>.build.tmp`，**反序列化成功才 `os.replace` 覆盖正式 engine**——trtexec 中途 OOM
  或被 Ctrl-C 不会把原本还能用的 engine 弄没。
- 重建后 `trt_backend.build_sidecar(...)` 写 sidecar（含输出 binding 名/形状），再
  `verify_engine()` 复验一次。

## 3. 脚本二：`scripts/deploy_board.ps1`（Windows，一键同步 + 校验）

```powershell
pwsh -File scripts\deploy_board.ps1              # 同步 + sha256 校验 + check_backend + run_tests
pwsh -File scripts\deploy_board.ps1 -DryRun      # 只打印计划，不写板端
pwsh -File scripts\deploy_board.ps1 -SkipTests   # 跳过最后的单测（check_backend 照跑）
pwsh -File scripts\deploy_board.ps1 -Force       # 即使清单一致也重新上传
pwsh -File scripts\deploy_board.ps1 -Host 192.168.1.9 -WorkDir /home/jetson/other
```

| 参数 | 默认 | 说明 |
|---|---|---|
| `-Host` | `10.77.156.4` | 板子 IP（参数名实际是 `RemoteHost`，`-Host` 是别名：`$Host` 是 PS 只读自动变量） |
| `-User` | `jetson` | 板端账号 |
| `-WorkDir` | `/home/jetson/sentinel-quality-20261106` | 板端工作目录 |
| `-SkipTests` | 关 | 只跳过 `python3 -m vision.run_tests` |
| `-DryRun` | 关 | 只打印计划（含"会不会跳过上传"），不动板端 |
| `-Force` | 关 | 强制重传（默认清单一致就跳过上传） |

做六步，任一步失败就**非零退出并给中文原因**：

1. 本机前置：`ssh/scp/tar` 在不在、`vision/` `scripts/` 在不在；
2. 板端连通：`cd` 进工作目录，打印 pwd / hostname / python3 版本；
3. 本机打包：`vision/` + `scripts/` → `tar.gz`（**不传** `logs/` `datasets/` `snapshots/`
   `vision/face_db/` `.git/` `__pycache__/`），按包内清单本机算 sha256；
4. 上传：`scp` tar → 板端先核 **tar 自己的 sha256**（防截断/串板）→ 解包 → 传清单；
5. 校验：板端 `sha256sum -c .board-sync.sha256`，有任何 `FAILED` 立即失败；
6. 自检：`python3 scripts/check_backend.py --profile board`（必须 exit 0）→
   `python3 -m vision.run_tests`（必须 OK，失败打印末尾 30 行）。

退出码：`0` 成功；`1` 连接/打包/上传/校验失败；`2` check_backend 不通过；`3` 单测不通过。

认证：板端免密公钥，所有 ssh/scp 都带 `BatchMode=yes`，**脚本里没有也不接受密码**；
板端一旦开始要密码，脚本直接失败而不是挂着等输入。

幂等：可反复跑；清单一致（板端逐个 sha256 通过 + 清单文件哈希一致）时跳过上传；
同步是**覆盖式**，不删除板端多余文件（不用 `rsync --delete`：板端的 `material-eval/`、
`snapshots/` 都是跑出来的产物，误删代价大）。

## 4. 板端实测（2026-11-07）

**`ensure_engines.py --check`**：exit 0，0.3 s

```
[engine] 检测 yunet_fp16.engine（源模型 yunet_trt320.onnx）
  OK（TensorRT 8.2.1.8 / fp16 / min=1x3x320x320 opt=1x3x320x320 max=1x3x320x320）
[engine] 识别 sface_batch_fp16.engine（源模型 sface_dynamic_batch.onnx）
  OK（TensorRT 8.2.1.8 / fp16 / min=1x3x112x112 opt=4x3x112x112 max=8x3x112x112）
  2/2 可用；重建 0 个；总耗时 0.3 s
```

**重建路径实测**（在 `/tmp/engprobe/` 里做，生产 engine 哈希前后都是
`f5908efa…b354e`，没被碰）：把 `yunet_fp16.engine` 复制成 `x.engine`（sidecar 也改名），
`--engine /tmp/engprobe/x.engine --model .../yunet_trt320.onnx --force`：

```
trtexec: /usr/src/tensorrt/bin/trtexec --onnx=.../yunet_trt320.onnx \
         --saveEngine=/tmp/engprobe/x.engine.build.tmp --fp16
  重建完成（耗时 150.6 s）
  1/1 可用；重建 1 个；总耗时 155.7 s      ← trtexec 2 分半，Nano 上正常
```

重建后 `--check` 复验 exit 0；新 engine 999,154 B（原 996,158 B，同版本 trtexec 重建的产物字节
不保证一致，所以必须靠 sidecar 哈希而不是"大小看起来差不多"）；新 sidecar 的输出 binding
（`loc/conf/iou`）与原来一致。**在 /tmp 试验的姿势**：

```bash
mkdir -p /tmp/engprobe && cp yunet_fp16.engine /tmp/engprobe/x.engine
cp yunet_fp16.engine.json /tmp/engprobe/x.engine.json
python3 scripts/ensure_engines.py --engine /tmp/engprobe/x.engine \
    --model ~/sentinel-quality-20261106/yunet_trt320.onnx --force
```

**`deploy_board.ps1` 实测**：exit 0。同步 99 个文件 / 35.25 MB（`vision/` + `scripts/`，含
`vision/models/*.onnx`、`vision/tests/`、`vision/config/`）；`sha256 校验通过：99/99`；
`check_backend` exit 0（`yunet-onnx/tensorrt + sface-onnx/tensorrt`）；
`Ran 352 tests in 8.923s  OK (skipped=2)`。连续第二次跑 → "板端清单与本次完全一致 → 跳过上传"，
校验与自检照跑，exit 0；`-Force` → 重新上传后仍是 99/99；`-WorkDir` 写错 → exit 1 且提示排查
顺序（实测输出见脚本注释里的退出码约定）。

## 5. 失败排查

| 现象 | 原因 | 处理 |
|---|---|---|
| `找不到 trtexec` | 只有 runtime 没装 dev 包，或不在 PATH | `ls /usr/src/tensorrt/bin/trtexec`；有就用 `--trtexec` 指定；没有就 `sudo apt-get install nvidia-tensorrt-dev`（JP46 源） |
| trtexec 退出码非 0 / 卡住 | 内存不够（Nano 4G，TRT 建 engine 要几百 MB～1G） | `free -m` 看 available/swap；关掉板端其他 python（尤其实时管线）；**一次只重建一个**（脚本本身按顺序做，别手动并行跑两个 trtexec） |
| `TensorRT 版本不匹配：engine 建于 8.2.1.8，当前 X` | 换板/重刷 JetPack | `python3 scripts/ensure_engines.py --force`（必须重建，不能改 sidecar 骗过去） |
| `模型哈希不匹配（engine 不是从这份模型建的）` | sidecar 指向的 ONNX 换过 | 确认配置里的 ONNX 是要用的那份，再 `--force` 重建 |
| `engine 缺少 sidecar` | 只拷了 engine 没拷 `.json` | `--force` 重建（sidecar 会重新生成）；临时试验可先拷 `.json` 再用 `--check` |
| `需要重建 —— YuNet 必须是 320x320 输入` | 当初照 ONNX 声明的 120×160 建的 | `--force` 重建；脚本会用正确 profile |
| `check_backend 不通过：实际后端 …` | engine 漂移 → auto 静默退 CPU | 先 `ensure_engines.py --check` 看原因，再不带 `--check` 重跑重建 |
| `sha256 校验不通过：通过 N/99` | 上传中断、板端磁盘满、有人手改板端文件 | 修网络/清空间后重跑（覆盖式同步会修好）；**别跳过校验直接自检** |
| `连不上板端或工作目录不存在` | 不在同一网段 / 免密公钥失效 / `-WorkDir` 错 | 按脚本打印的三步排查顺序来 |
| 单测不过 | 本机测试与板端代码不一致 / 板端缺依赖 | 本机先 `py -3.12 -m vision.run_tests` 对照；脚本会打印板端末尾 30 行 |

## 6. 约束与已知限制

- **engine 与 TensorRT 版本、设备强绑定**：换板或升级 JetPack 后第一件事就是
  `python3 scripts/ensure_engines.py --force`，然后 `deploy_board.ps1` 收尾自检。
- **别在板端直接重建生产 engine 做试验**：先 `cp` 到 `/tmp`，用 `--engine/--model`（脚本的
  `.build.tmp` + 原子替换能兜住失败，但试验仍建议隔离）。
- 同步范围只有 `vision/` + `scripts/`：板端根目录的 `yunet_trt320.onnx`、
  `sface_dynamic_batch.onnx`、`*_fp16.engine`（及 sidecar）是**历史产物、不在同步范围**，
  `board.yaml` 里用绝对路径指它们。
- 同步是覆盖式，不删除板端多余文件；新增的板端专用文件（比如 `*-probe.py`）下次同步仍在。
- Windows 的 tar(bsdtar) 不写 Unix 执行位：板端 `.sh` 需要执行位时自己 `chmod +x`；
  本流程全部走 `python3 script.py`，不依赖执行位。
- 板端 Python 3.6.9：新脚本都按 3.6 写（无 `dataclasses`、无 `:=`、`open(..., encoding=)` 可用）。
- 部署脚本不碰 `/dev/video0`：`check_backend` 只建检测/识别组件，单测用假 VideoCapture。