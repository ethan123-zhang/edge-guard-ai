"""部署自检：确认这块板子真的在用 TensorRT，而不是悄悄退到了 CPU dnn。

**为什么需要它**：`runtime.backend: auto` 的设计取舍是「engine 不可用时照跑，退 CPU」。
代价是**静默降级** —— 服务不会崩，只是从 17.6 fps 掉到 3.7 fps，唯一线索是日志。
部署和巡检时跑这个脚本，把「退化了」变成部署失败，而不是等用户觉得卡了才发现。

用法：
    # 部署后（要求 TensorRT；不满足就非 0 退出）
    python3 scripts/check_backend.py --profile board

    # 只想确认能跑起来（不关心走哪条路）
    python3 scripts/check_backend.py --profile board --expect any

    # 反例演练：验证回退链路确实生效（现场排查用）
    python3 scripts/check_backend.py --profile board --expect dnn

退出码：0 = 符合预期；1 = 建起来了但不是期望的后端；2 = 管线根本建不起来。
"""
import argparse
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from vision import config, onnx_pipeline


def backend_label(detector, recognizer):
    """`yunet-onnx/tensorrt + sface-onnx/tensorrt` 这种一眼能读的标签。"""
    parts = []
    for item in (detector, recognizer):
        name = str(getattr(item, "backend", "?"))
        accelerator = getattr(item, "accelerator", None)
        parts.append("%s/%s" % (name, accelerator) if accelerator else name)
    return " + ".join(parts)


def accelerators(detector, recognizer):
    return [getattr(item, "accelerator", None) for item in (detector, recognizer)]


def main(argv=None):
    ap = argparse.ArgumentParser(description="哨兵管家 · 实际后端自检")
    ap.add_argument("--profile", default="board", help="配置档（默认 board）")
    ap.add_argument("--config-dir", default=None, help="配置目录（默认 vision/config）")
    ap.add_argument("--expect", default="tensorrt", choices=("tensorrt", "dnn", "any"),
                    help="期望的加速后端：tensorrt（默认）/ dnn / any")
    args = ap.parse_args(argv)

    cfg = config.load(args.profile, args.config_dir)
    config.validate(cfg)
    print("[check] profile=%s runtime.backend=%s"
          % (args.profile, cfg["runtime"]["backend"]))

    try:
        detector, recognizer, mode = onnx_pipeline.create_components(cfg, log=print)
    except Exception as exc:      # 建不起来就是部署问题，别用 traceback 淹没人
        print("[check] 失败：管线建不起来 —— %s" % exc)
        return 2

    print("[check] mode=%s" % mode)
    print("[check] 检测：%s" % detector.describe())
    print("[check] 识别：%s" % recognizer.describe())
    label = backend_label(detector, recognizer)
    print("[check] 实际后端：%s" % label)

    if args.expect == "any":
        print("[check] 通过（不限定后端）")
        return 0

    got = accelerators(detector, recognizer)
    if args.expect == "tensorrt":
        ok = all(item == "tensorrt" for item in got)
        hint = "检测和识别都要走 TensorRT；engine 缺失/哈希或版本不符会退 CPU dnn，先重建 engine"
    else:
        ok = "tensorrt" not in got
        hint = "期望纯 CPU dnn，但现在有组件在用 TensorRT"
    if ok:
        print("[check] 通过：%s" % args.expect)
        return 0
    print("[check] 不通过：期望 %s，实际 %r —— %s" % (args.expect, got, hint))
    return 1


if __name__ == "__main__":
    sys.exit(main())