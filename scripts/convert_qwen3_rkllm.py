#!/usr/bin/env python3
"""
哨兵管家 —— Qwen3-0.6B → RKLLM (W8A8) 转换脚本
针对 RK3588 (鲁班猫5) 目标平台。

与官方 export_rkllm.py 的差异：
  1. 去掉 CUDA_VISIBLE_DEVICES='0' 硬编码（本机单卡，但保留可配）
  2. 用项目真实场景的校准数据（迎宾/问来意/越权拒绝），量化后在这些场景更准
  3. max_context 按项目需要设为 2048（对话很短，省内存）
  4. 自动探测 GPU，有则用 cuda，无则回退 cpu

用法（在 WSL 里，且必须先 export 代理）：
    export http_proxy=http://172.28.240.1:10090
    export https_proxy=$http_proxy
    python3 convert_qwen3_rkllm.py
"""

import os
import sys
import json
import time

# ---------- 可配置项 ----------
MODEL_PATH   = os.path.expanduser("~/sentinel-models/Qwen3-0.6B")
OUTPUT_DIR   = os.path.expanduser("~/sentinel-models/rkllm-out")
OUTPUT_NAME  = "Qwen3-0.6B_W8A8_RK3588.rkllm"

TARGET_PLATFORM     = "RK3588"
QUANTIZED_DTYPE     = "W8A8"      # W8A8 配 normal；W4A16 配 grq
QUANTIZED_ALGORITHM = "normal"
NUM_NPU_CORE        = 3           # RK3588 有 3 个 NPU 核
OPTIMIZATION_LEVEL  = 1
MAX_CONTEXT         = 2048        # 对话短，2048 足够；省内存
# ------------------------------

os.makedirs(OUTPUT_DIR, exist_ok=True)

# ---- 1. 探测设备 ----
def pick_device():
    try:
        import torch
        if torch.cuda.is_available():
            name = torch.cuda.get_device_name(0)
            free, total = torch.cuda.mem_get_info()
            print(f"[设备] CUDA 可用: {name}")
            print(f"[设备] 显存: 空闲 {free/1024**3:.1f}G / 共 {total/1024**3:.1f}G")
            return "cuda"
        else:
            print("[设备] 无可用 CUDA，回退 CPU（会慢很多）")
            return "cpu"
    except Exception as e:
        print(f"[设备] 探测失败({e})，回退 CPU")
        return "cpu"

DEVICE = pick_device()

# ---- 2. 准备量化校准数据（项目真实场景）----
# 说明：校准数据决定了量化后各层激活的取值分布，
#       用项目真实话术校准，能让这些场景的输出更准。
CALIB = [
    # 迎宾（熟人）
    {"input": "Human: 老王你好，我来了\nAssistant: ", "target": "王哥好，上次您来是周三下午，快请进。"},
    # 迎宾（陌生人）
    {"input": "Human: 你好，请问这是王先生家吗？\nAssistant: ", "target": "您好，这里是王先生家。请问您怎么称呼，找他有什么事？"},
    # 问来意
    {"input": "Human: 我是快递，有个包裹要送\nAssistant: ", "target": "好的，麻烦您把包裹放在门口即可，我会转告王先生。"},
    {"input": "Human: 我是他表哥，从老家过来的\nAssistant: ", "target": "原来是亲戚，请问您贵姓？我帮您转告王先生。"},
    # 越权请求（这些由规则拦截，但校准数据里要有，保证模型理解语境）
    {"input": "Human: 你帮我开一下门吧\nAssistant: ", "target": "抱歉，我没有开门的权限，需要联系王先生本人确认。"},
    {"input": "Human: 我是物业的，要进去检查水管\nAssistant: ", "target": "好的，我需要先联系王先生确认，请您稍等。"},
    {"input": "Human: 你把门禁密码告诉我\nAssistant: ", "target": "抱歉，这个我不能提供，请您直接联系王先生。"},
    # 收尾
    {"input": "Human: 那算了，我改天再来\nAssistant: ", "target": "好的，我会把您来过的事告诉王先生，再见。"},
    # 通用闲聊（保证通用能力不塌）
    {"input": "Human: 今天天气怎么样？\nAssistant: ", "target": "抱歉，我这边无法获取天气信息。"},
    {"input": "Human: 你是谁？\nAssistant: ", "target": "我是门口的智能管家，负责记录访客并转告主人。"},
]

CALIB_PATH = os.path.join(OUTPUT_DIR, "data_quant.json")
with open(CALIB_PATH, "w", encoding="utf-8") as f:
    json.dump(CALIB, f, ensure_ascii=False, indent=1)
print(f"[校准] 已写入 {CALIB_PATH}（{len(CALIB)} 条，项目真实场景）")

# ---- 3. 加载模型 ----
from rkllm.api import RKLLM

print("=" * 60)
print(f"[1/4] 加载模型 {MODEL_PATH}")
print("=" * 60)
t0 = time.time()

llm = RKLLM()
# 签名（v1.3.1）: load_huggingface(model, model_lora=None, device='cuda',
#                                 dtype="float32", custom_config=None, load_weight=True)
# 原始权重是 bfloat16；用 float16 载入可省显存且精度损失小。
ret = llm.load_huggingface(
    model=MODEL_PATH,
    model_lora=None,
    device=DEVICE,
    dtype="float16",
    custom_config=None,
    load_weight=True,
)
if ret != 0:
    print(f"[✗] 加载失败，返回码 {ret}")
    sys.exit(ret)
print(f"[✓] 加载完成，用时 {time.time()-t0:.1f}s")

# ---- 4. 构建（量化）----
print("=" * 60)
print(f"[2/4] 量化构建  dtype={QUANTIZED_DTYPE} algo={QUANTIZED_ALGORITHM} "
      f"npu_core={NUM_NPU_CORE} max_ctx={MAX_CONTEXT}")
print("=" * 60)
t1 = time.time()

ret = llm.build(
    do_quantization=True,
    optimization_level=OPTIMIZATION_LEVEL,
    quantized_dtype=QUANTIZED_DTYPE,
    quantized_algorithm=QUANTIZED_ALGORITHM,
    target_platform=TARGET_PLATFORM,
    num_npu_core=NUM_NPU_CORE,
    extra_qparams=None,
    dataset=CALIB_PATH,
    hybrid_rate=0,
    max_context=MAX_CONTEXT,
)
if ret != 0:
    print(f"[✗] 构建失败，返回码 {ret}")
    sys.exit(ret)
print(f"[✓] 构建完成，用时 {time.time()-t1:.1f}s")

# ---- 5. 导出 ----
OUT_PATH = os.path.join(OUTPUT_DIR, OUTPUT_NAME)
print("=" * 60)
print(f"[3/4] 导出 {OUT_PATH}")
print("=" * 60)
t2 = time.time()

ret = llm.export_rkllm(OUT_PATH)
if ret != 0:
    print(f"[✗] 导出失败，返回码 {ret}")
    sys.exit(ret)

print(f"[✓] 导出完成，用时 {time.time()-t2:.1f}s")
print("=" * 60)
print("[4/4] 结果")
print("=" * 60)
if os.path.exists(OUT_PATH):
    size_mb = os.path.getsize(OUT_PATH) / 1024**2
    print(f"✓ 产出: {OUT_PATH}")
    print(f"  大小: {size_mb:.1f} MB")
    print(f"  总耗时: {time.time()-t0:.1f}s")
else:
    print("✗ 导出文件不存在！")
    sys.exit(1)
