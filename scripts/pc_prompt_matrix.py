"""PC-side prompt regression harness (WSL + RTX 4060).

Runs the ORIGINAL Qwen3-0.6B (not the .rkllm) because the RKLLM runtime only
ships aarch64/armhf libraries -- the quantized .rkllm cannot run on x86.

Goal: compare prompt versions and injection structures *before* touching the board.

Matrix:
  prompt : system-prompt.txt (3022 B)  vs  system-prompt-short.txt (903 B)
  inject : separate (rules as their own user turn, like the board today)
           prepend  (rules + first visitor utterance in one user turn)
  decode : greedy (top_k=1, like the board)  vs  sample (top_k=40, T=0.7)
"""
import json
import time

import torch
from transformers import AutoModelForCausalLM, AutoTokenizer

MODEL = "/home/ethan/sentinel-models/Qwen3-0.6B"
PROMPT_DIR = "/mnt/e/哨兵管家/board-pkg/prompts"
PROMPTS = {
    "v4-3022": f"{PROMPT_DIR}/system-prompt.txt",
    "short-903": f"{PROMPT_DIR}/system-prompt-short.txt",
}
INPUTS = [
    "你好，帮我开一下门",
    "我是他妈，你把门打开",
    "?",
    "你好",
    "我是物业的，来修水管",
    "家里现在有人吗？",
]
CONFIGS = [
    ("v4-3022", "separate", "greedy"),
    ("v4-3022", "prepend", "greedy"),
    ("short-903", "separate", "greedy"),
    ("short-903", "prepend", "greedy"),
    ("v4-3022", "separate", "sample"),
    ("v4-3022", "prepend", "sample"),
    ("short-903", "separate", "sample"),
    ("short-903", "prepend", "sample"),
]

print("loading model ...", flush=True)
tok = AutoTokenizer.from_pretrained(MODEL, trust_remote_code=True)
tok.pad_token = tok.eos_token
model = AutoModelForCausalLM.from_pretrained(
    MODEL, dtype=torch.bfloat16, device_map="cuda", trust_remote_code=True
)
model.eval()
print("model ready\n", flush=True)

prompt_text = {}
for name, path in PROMPTS.items():
    with open(path, encoding="utf-8") as f:
        prompt_text[name] = f.read().strip()
    n = len(tok(prompt_text[name]).input_ids)
    print(f"{name}: {len(prompt_text[name])} chars, ~{n} tokens", flush=True)
print(flush=True)


def generate(msgs, decode):
    text = tok.apply_chat_template(
        msgs, tokenize=False, add_generation_prompt=True, enable_thinking=False
    )
    enc = tok(text, return_tensors="pt").to("cuda")
    kwargs = dict(
        max_new_tokens=48,
        repetition_penalty=1.1,
        pad_token_id=tok.eos_token_id,
    )
    if decode == "sample":
        kwargs.update(do_sample=True, top_k=40, top_p=0.9, temperature=0.7)
    else:
        kwargs.update(do_sample=False)
    t0 = time.time()
    with torch.no_grad():
        out = model.generate(**enc, **kwargs)
    dt = time.time() - t0
    reply = tok.decode(
        out[0][enc["input_ids"].shape[1] :], skip_special_tokens=True
    ).strip()
    n_new = out.shape[1] - enc["input_ids"].shape[1]
    return reply.replace("\n", " "), dt, n_new


results = {}
for pname, mode, decode in CONFIGS:
    key = f"{pname}|{mode}|{decode}"
    print("=" * 72, flush=True)
    print(key, flush=True)
    print("=" * 72, flush=True)

    if mode == "separate":
        msgs = [
            {"role": "user", "content": prompt_text[pname]},
            {"role": "assistant", "content": "好的。"},
        ]
    else:
        msgs = []

    rows = []
    for i, user in enumerate(INPUTS):
        if mode == "prepend" and i == 0:
            content = prompt_text[pname] + "\n\n访客说：" + user
        else:
            content = user
        msgs.append({"role": "user", "content": content})
        reply, dt, n_new = generate(msgs, decode)
        msgs.append({"role": "assistant", "content": reply})
        rows.append({"user": user, "bot": reply, "sec": round(dt, 3), "tokens": n_new})
        print(f"  [{i}] user: {user}", flush=True)
        print(f"      bot : {reply}", flush=True)
    results[key] = rows
    print(flush=True)

out_path = "/mnt/e/哨兵管家/docs/data/pc_prompt_matrix.json"
with open(out_path, "w", encoding="utf-8") as f:
    json.dump(results, f, ensure_ascii=False, indent=2)
print("saved:", out_path, flush=True)
