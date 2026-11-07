"""Engine 校验 → 缺了/漂了重建 → 复验：换板、升级 JetPack 后一条命令拉回可用。

**为什么需要这个脚本**
`runtime.backend: auto` 的取舍是「engine 不可用就退 CPU dnn」：服务不崩，只是从
17.6 fps 掉到 3.7 fps，唯一线索是日志。engine 在 sidecar（`<engine>.json`，
schema `sentinel/trt-engine@1`）里绑了三样东西：engine 自身 sha256、源模型 sha256、
建它时的 TensorRT 版本（见 `vision/trt_backend.py:verify_engine`）。任一漂移就抛
`BackendUnavailable` → **静默**回退 CPU。现场最常见的两种：

1. 换板 / 重刷 JetPack：TensorRT 版本变了（8.2.1.8 → 别的），engine 无法反序列化；
2. 模型换了（哪怕只差几个字节）：模型哈希对不上，engine 不能再信任。

本脚本对每个 engine 做三件事：`verify_engine()` 校验 → 不通过就用 trtexec 重建 →
写 sidecar 后再 `verify_engine()` 复验。重建的输入 profile **一律从现有 sidecar 的
`input` 字段读**（这是唯一记录「当时按什么形状建的」的地方），sidecar 丢了才退回内置
约定值，并在输出里明说。**不靠猜**：YuNet 固定 320×320（ONNX 里声明的 120×160 是
历史遗留，照它建出来的 engine 一喂 320 就维度越界），SFace 112×112、batch 1..8。

**用法**
    # 只体检（部署前/巡检）；需要重建时 exit 1
    python3 scripts/ensure_engines.py --check

    # 按 board.yaml 的 runtime.detector_engine / recognizer_engine：缺的/漂的重建
    python3 scripts/ensure_engines.py

    # 换板后第一次：无条件重建
    python3 scripts/ensure_engines.py --force

    # 在 /tmp 试验重建路径，绝对不碰正式 engine
    cp yunet_fp16.engine yunet_fp16.engine.json /tmp/engprobe/ && \
        mv /tmp/engprobe/yunet_fp16.engine /tmp/engprobe/x.engine && \
        mv /tmp/engprobe/yunet_fp16.engine.json /tmp/engprobe/x.engine.json
    python3 scripts/ensure_engines.py --engine /tmp/engprobe/x.engine \
        --model ~/sentinel-quality-20261106/yunet_trt320.onnx --force

板端跑之前照例补线程环境（本脚本在 arm 上会自己补齐，见 `_board_env`）：
    export OPENBLAS_CORETYPE=ARMV8 OPENBLAS_NUM_THREADS=1 OMP_NUM_THREADS=1

**退出码**：0 = 全部可用（或本来就不需要重建）；1 = 有 engine 不可用 / `--check` 下
需要重建 / 重建失败；2 = 配置读不了。
"""
import argparse
import copy
import json
import os
import platform
import shutil
import subprocess
import sys
import time

# 板端 numpy 的线程环境：只在 ARM 上补默认值（PC 上设 OPENBLAS_CORETYPE=ARMV8 会直接
# 让 OpenBLAS 报 "This system/CPU type is not supported"，所以必须按架构判断）。
def _board_env():
    if platform.machine().lower() in ("aarch64", "armv7l", "armv8l"):
        os.environ.setdefault("OPENBLAS_CORETYPE", "ARMV8")
        os.environ.setdefault("OPENBLAS_NUM_THREADS", "1")
        os.environ.setdefault("OMP_NUM_THREADS", "1")


_board_env()

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from vision import config, trt_backend  # noqa: E402  （要先补好板端环境再 import numpy）

SCHEMA_VERSION = "sentinel/ensure-engines@1"

# trtexec 的查找顺序：JetPack 自带的固定路径优先，再试 PATH。
TRTEXEC_PATHS = ("/usr/src/tensorrt/bin/trtexec",)

# sidecar 丢了才用这些内置约定（值来自 `docs/Jetson-TRT加速评估-2026-11-06.md` 与
# 板上现有 sidecar，2026-11-07 核对）。sidecar 在时永远优先读 sidecar。
DEFAULT_INPUTS = {
    "detector": {
        "name": "input",
        "profile": {"min": [1, 3, 320, 320], "opt": [1, 3, 320, 320], "max": [1, 3, 320, 320]},
        "why": "YuNet 固定 320x320（ONNX 里声明的 120x160 是历史遗留，别照它建）",
    },
    "recognizer": {
        "name": "data",
        "profile": {"min": [1, 3, 112, 112], "opt": [4, 3, 112, 112], "max": [8, 3, 112, 112]},
        "why": "SFace 112x112，batch 上限 8（与 runtime.recognizer_batch 的上限一致）",
    },
}


class BuildError(RuntimeError):
    """重建链路自身失败（trtexec 找不到/退出码非 0/新 engine 打不开）。"""


def log(message):
    print(message)
    sys.stdout.flush()


# ---------------------------------------------------------------- 形状小工具

def _shape(profile, key):
    return tuple(int(v) for v in (profile.get(key) or []))


def profile_dynamic(profile):
    low, high = _shape(profile, "min"), _shape(profile, "max")
    return len(low) != len(high) or any(a != b for a, b in zip(low, high))


def profile_text(profile):
    parts = []
    for key in ("min", "opt", "max"):
        shape = _shape(profile, key)
        if shape:
            parts.append("%s=%s" % (key, "x".join(str(v) for v in shape)))
    return " ".join(parts)


def _shape_arg(shape):
    return "x".join(str(v) for v in shape)


def _complete_profile(profile):
    """min/opt/max 缺一个就补出来（opt 取中点），trtexec 三个都要。"""
    profile = copy.deepcopy(profile or {})
    low, high = _shape(profile, "min"), _shape(profile, "max")
    if not low or not high or len(low) != len(high):
        return None
    opt = _shape(profile, "opt")
    if len(opt) != len(low):
        opt = tuple((a + b) // 2 for a, b in zip(low, high))
    return {"min": list(low), "opt": list(opt), "max": list(high)}


def profile_problem(role, profile):
    """按约定核对 profile；没问题返回 None，有问题返回中文原因。

    这是 `verify_engine()` 管不到的一类漂移：哈希和版本都对，但当初按错的形状建的
    （比如照 ONNX 声明的 120×160 建了 YuNet），运行到第一帧才炸。
    """
    if role not in DEFAULT_INPUTS:
        return None
    low, high = _shape(profile, "min"), _shape(profile, "max")
    if not low or not high or len(low) != len(high):
        return "profile 的 min/max 不完整：%s" % profile_text(profile)
    if role == "detector":
        if tuple(low[1:]) != (3, 320, 320) or tuple(high[1:]) != (3, 320, 320):
            return "YuNet 必须是 320x320 输入，sidecar 记的是 %s" % profile_text(profile)
        if low[0] != 1 or high[0] != 1:
            return "YuNet batch 必须固定为 1，sidecar 记的是 %s" % profile_text(profile)
    else:
        if tuple(low[1:]) != (3, 112, 112) or tuple(high[1:]) != (3, 112, 112):
            return "SFace 必须是 112x112 输入，sidecar 记的是 %s" % profile_text(profile)
        if not (1 <= low[0] <= high[0] <= 8):
            return "SFace batch 必须落在 1..8，sidecar 记的是 %s" % profile_text(profile)
    return None


# ---------------------------------------------------------------- sidecar / engine 读取

def read_sidecar(engine_path):
    """尽力读 sidecar；读不了返回 None（校验失败的事实由 verify_engine 负责报）。"""
    path = trt_backend.sidecar_path(engine_path)
    try:
        with open(path, "r", encoding="utf-8") as fh:
            return json.load(fh)
    except (ValueError, OSError):
        return None


def input_entry(engine_path, role):
    """返回 (input_entry, 来源说明)。sidecar 里有 input 就信 sidecar。"""
    sidecar = read_sidecar(engine_path)
    entry = (sidecar or {}).get("input")
    if isinstance(entry, dict) and entry.get("name") and entry.get("profile"):
        profile = _complete_profile(entry["profile"])
        if profile is not None:
            return {"name": str(entry["name"]), "profile": profile}, "sidecar"
    if role in DEFAULT_INPUTS:
        fallback = copy.deepcopy(DEFAULT_INPUTS[role])
        fallback.pop("why", None)
        return fallback, "内置约定"
    return None, "无法确定"


def engine_outputs(engine_path):
    """反序列化 engine 读出输出 binding 名与形状（顺带证明它真能加载）。"""
    import tensorrt as trt
    with open(str(engine_path), "rb") as fh:
        engine = trt.Runtime(trt.Logger(trt.Logger.WARNING)).deserialize_cuda_engine(fh.read())
    if engine is None:
        raise BuildError("engine 反序列化返回 None（文件损坏或 TensorRT 版本不匹配）")
    outputs = []
    for index in range(engine.num_bindings):
        if engine.binding_is_input(index):
            continue
        outputs.append({"name": engine.get_binding_name(index),
                        "shape": [int(v) for v in engine.get_binding_shape(index)]})
    return outputs


def infer_role(engine_path, model_path):
    """`--engine/--model` 单文件模式：从文件名判断是检测还是识别。"""
    text = ("%s %s" % (os.path.basename(str(engine_path or "")),
                       os.path.basename(str(model_path or "")))).lower()
    if "sface" in text:
        return "recognizer"
    if "yunet" in text or "face_detection" in text:
        return "detector"
    return None


# ---------------------------------------------------------------- 校验 / 重建

def inspect_engine(engine_path, model_path, role, want_fp16):
    """返回 None 表示可用，否则返回「需要重建」的中文原因。"""
    if not os.path.exists(engine_path):
        return "engine 不存在：%s" % engine_path
    if not model_path:
        return "配置里没有源模型路径，无法核对 engine 绑的是哪份模型"
    if not os.path.exists(model_path):
        return "源模型不存在：%s" % model_path
    try:
        sidecar = trt_backend.verify_engine(engine_path, model_path)
    except trt_backend.BackendUnavailable as exc:
        return str(exc)
    if bool(sidecar.get("fp16")) != bool(want_fp16):
        return "sidecar 记的精度是 fp16=%s，本次要求 fp16=%s" % (
            bool(sidecar.get("fp16")), bool(want_fp16))
    entry = sidecar.get("input") or {}
    problem = profile_problem(role, entry.get("profile") or {})
    if problem:
        return problem
    return None


def resolve_trtexec(explicit):
    if explicit:
        if os.path.exists(explicit) and os.access(explicit, os.X_OK):
            return explicit
        raise BuildError("指定的 trtexec 不可执行：%s" % explicit)
    for candidate in TRTEXEC_PATHS:
        if os.path.exists(candidate) and os.access(candidate, os.X_OK):
            return candidate
    found = shutil.which("trtexec")
    if found:
        return found
    raise BuildError(
        "找不到 trtexec：JetPack 自带的在 /usr/src/tensorrt/bin/trtexec，"
        "PATH 里也没有。确认 TensorRT 装了（dpkg -l | grep -i tensorrt），"
        "或用 --trtexec 指定绝对路径")


def build_command(trtexec, model_path, target_path, entry, fp16):
    """拼 trtexec 命令。静态 profile 不传 *Shapes（历史记录里 YuNet 就是这么建的）。"""
    cmd = [trtexec, "--onnx=%s" % model_path, "--saveEngine=%s" % target_path]
    if fp16:
        cmd.append("--fp16")
    if profile_dynamic(entry["profile"]):
        for key, flag in (("min", "--minShapes"), ("opt", "--optShapes"), ("max", "--maxShapes")):
            cmd.append("%s=%s:%s" % (flag, entry["name"], _shape_arg(_shape(entry["profile"], key))))
    return cmd


def _tail(text, count=20):
    lines = [line for line in text.splitlines() if line.strip()]
    return "\n".join(lines[-count:])


def rebuild_engine(engine_path, model_path, role, fp16, trtexec):
    """重建一个 engine：先建到 `.build.tmp`，成功且能反序列化才替换正式文件。

    这样 trtexec 中途失败（OOM/被 Ctrl-C）不会把原本还能用的 engine 弄没。
    返回 (耗时秒数, 新建的 sidecar)。
    """
    entry, source = input_entry(engine_path, role)
    if entry is None:
        raise BuildError("读不到输入 profile，且无法从配置推断：%s" % engine_path)
    if source != "sidecar":
        log("  [warn] 没有可用 sidecar 的 input 字段，输入 profile 用内置约定：%s"
            % profile_text(entry["profile"]))
    elif role in DEFAULT_INPUTS:
        problem = profile_problem(role, entry["profile"])
        if problem:
            # sidecar 自己记的 profile 就是错的：照它重建只会复制错误，用约定值。
            log("  [warn] sidecar 的 input profile 不符合约定（%s），改用内置正确值重建"
                % problem)
            entry["profile"] = copy.deepcopy(DEFAULT_INPUTS[role]["profile"])
    entry["profile"] = _complete_profile(entry["profile"])
    if entry["profile"] is None:
        raise BuildError("输入 profile 不完整：%s" % profile_text(entry))
    if trtexec is None:
        trtexec = resolve_trtexec(None)

    tmp_path = engine_path + ".build.tmp"
    if os.path.exists(tmp_path):
        os.remove(tmp_path)
    cmd = build_command(trtexec, model_path, tmp_path, entry, fp16)
    log("  trtexec: %s" % " ".join(cmd))
    started = time.time()
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    output = proc.communicate()[0].decode("utf-8", "replace")
    seconds = time.time() - started
    try:
        if proc.returncode != 0:
            raise BuildError("trtexec 退出码 %d，末尾输出：\n%s"
                             % (proc.returncode, _tail(output)))
        if not os.path.exists(tmp_path) or os.path.getsize(tmp_path) == 0:
            raise BuildError("trtexec 退出码 0 但没有产出 engine（%s）" % tmp_path)
        try:
            outputs = engine_outputs(tmp_path)
        except Exception as exc:      # tensorrt 导入/反序列化失败都要在替换前拦住
            raise BuildError("新 engine 反序列化失败（换 board 或升级 JetPack 后常见）：%s"
                             % exc)
        os.replace(tmp_path, engine_path)
    finally:
        if os.path.exists(tmp_path):
            try:
                os.remove(tmp_path)
            except OSError:
                pass
    sidecar = trt_backend.build_sidecar(engine_path, model_path, entry["name"],
                                        entry["profile"], fp16=fp16, outputs=outputs)
    trt_backend.verify_engine(engine_path, model_path)   # 复验：哈希/版本/模型三样再核一遍
    return seconds, sidecar


# ---------------------------------------------------------------- 目标收集

def targets_from_config(profile, config_dir):
    cfg = config.load(profile, config_dir)
    runtime = cfg["runtime"]
    detection_model = str(runtime.get("detector_onnx") or "").strip()
    recognition_model = str(cfg["recognition"].get("model") or "").strip()
    targets = []
    for role, label, key in (("detector", "检测", "runtime.detector_engine"),
                             ("recognizer", "识别", "runtime.recognizer_engine")):
        raw = str(runtime.get(role + "_engine") or "").strip()
        model = detection_model if role == "detector" else recognition_model
        targets.append({
            "role": role,
            "label": label,
            "key": key,
            "engine": config.resolve_vision(raw) if raw else "",
            "model": config.resolve_vision(model) if model else "",
        })
    return cfg, targets


def targets_from_args(args):
    engine = os.path.abspath(os.path.expanduser(args.engine))
    model = os.path.abspath(os.path.expanduser(args.model)) if args.model else ""
    role = infer_role(engine, model)
    if role is None:
        log("[warn] 从文件名看不出是检测还是识别 engine，跳过 profile 约定核对"
            "（仍会校验哈希与 TensorRT 版本）")
    return None, [{
        "role": role,
        "label": "指定",
        "key": "--engine",
        "engine": engine,
        "model": model,
    }]


# ---------------------------------------------------------------- main

def main(argv=None):
    parser = argparse.ArgumentParser(
        description="哨兵管家 · TensorRT engine 校验/重建/复验（板端跑，py3.6 兼容）")
    parser.add_argument("--profile", default="board", help="配置档（默认 board）")
    parser.add_argument("--config-dir", default=None, help="配置目录（默认 vision/config）")
    parser.add_argument("--check", action="store_true",
                        help="只校验不重建；需要重建时 exit 1")
    parser.add_argument("--force", action="store_true", help="无条件重建（不看现有 engine）")
    precision = parser.add_mutually_exclusive_group()
    precision.add_argument("--fp16", dest="fp16", action="store_true", help="FP16（默认）")
    precision.add_argument("--fp32", dest="fp16", action="store_false", help="FP32")
    parser.set_defaults(fp16=True)
    parser.add_argument("--engine", default=None, help="只处理这一个 engine（配合 --model）")
    parser.add_argument("--model", default=None, help="该 engine 的源 ONNX（配合 --engine）")
    parser.add_argument("--trtexec", default=None, help="trtexec 路径（默认先找 JetPack 自带）")
    args = parser.parse_args(argv)

    if args.check and args.force:
        log("[ensure] --check 与 --force 互斥：--check 承诺不动文件，--force 要求重写。"
            "只体检请去掉 --force；要重建请去掉 --check。")
        return 2
    if args.engine and not args.model:
        log("[ensure] 用 --engine 时必须同时给 --model（sidecar 里的模型哈希要对着源模型核对）。")
        return 2
    if args.model and not args.engine:
        log("[ensure] 用 --model 时也要给 --engine；不指定 engine 就按 --profile 的两个 engine 处理。")
        return 2

    started = time.time()
    if args.engine:
        cfg, targets = targets_from_args(args)
        log("[ensure] 单文件模式：%s（不读配置文件）" % args.engine)
    else:
        try:
            cfg, targets = targets_from_config(args.profile, args.config_dir)
        except config.ConfigError as exc:
            log("[ensure] 配置读取失败：%s" % exc)
            return 2
        log("[ensure] profile=%s runtime.backend=%s 精度=%s%s"
            % (args.profile, cfg["runtime"].get("backend"),
               "fp16" if args.fp16 else "fp32", "（--check 只校验）" if args.check else ""))

    trtexec = None      # 需要重建时才解析：--check 不应该因为机器上没 trtexec 就失败

    statuses = []
    failures = 0
    pending = 0
    rebuilt = 0
    for target in targets:
        engine = target["engine"]
        label = target["label"]
        if not engine:
            log("[engine] %s：未配置（%s 为空），跳过" % (label, target["key"]))
            statuses.append((label, "未配置", None))
            continue

        log("[engine] %s %s（源模型 %s）"
            % (label, os.path.basename(engine), os.path.basename(target["model"]) or "未配置"))
        reason = inspect_engine(engine, target["model"], target["role"], args.fp16)
        if reason is None and not args.force:
            sidecar = read_sidecar(engine) or {}
            log("  OK（TensorRT %s / %s / %s）"
                % (sidecar.get("tensorrt"), "fp16" if sidecar.get("fp16") else "fp32",
                   profile_text((sidecar.get("input") or {}).get("profile") or {})))
            statuses.append((label, "OK", None))
            continue
        if reason is None:
            reason = "按 --force 无条件重建"

        if args.check:
            log("  需要重建 —— %s" % reason)
            statuses.append((label, "需要重建", None))
            pending += 1
            continue

        log("  需要重建 —— %s" % reason)
        try:
            if trtexec is None:
                trtexec = resolve_trtexec(args.trtexec)
            seconds, sidecar = rebuild_engine(engine, target["model"], target["role"],
                                              args.fp16, trtexec)
        except (BuildError, trt_backend.BackendUnavailable) as exc:
            log("  失败 —— %s" % exc)
            statuses.append((label, "失败", None))
            failures += 1
            continue
        log("  重建完成（耗时 %.1f s）" % seconds)
        statuses.append((label, "重建完成", seconds))
        rebuilt += 1

    elapsed = time.time() - started
    log("")
    log("=== 总结 ===")
    for label, status, seconds in statuses:
        suffix = "（%.1f s）" % seconds if seconds is not None else ""
        log("  %s：%s%s" % (label, status, suffix))
    usable = sum(1 for _, status, _ in statuses if status in ("OK", "重建完成"))
    log("  %d/%d 可用；重建 %d 个；总耗时 %.1f s"
        % (usable, len(statuses), rebuilt, elapsed))
    if failures:
        log("  结果：失败 —— 见上面的原因；engine 保持原样，生产侧会回退 CPU（慢约 8 倍）")
        return 1
    if pending:
        log("  结果：需要重建 —— 去掉 --check 重跑，或 python3 scripts/ensure_engines.py --force")
        return 1
    log("  结果：OK")
    return 0


if __name__ == "__main__":
    sys.exit(main())