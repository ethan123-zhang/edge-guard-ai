"""Single-turn independence test.

If each input is answered in a FRESH context, we can tell whether the model
can follow the prompt at all, or whether the multi-turn history (a bad first
reply) is what poisons the rest.
"""
import torch
from transformers import AutoModelForCausalLM, AutoTokenizer

MODEL = "/home/ethan/sentinel-models/Qwen3-0.6B"
PD = "/mnt/e/哨兵管家/board-pkg/prompts"
SHORT = open(f"{PD}/system-prompt-short.txt", encoding="utf-8").read().strip()
V4 = open(f"{PD}/system-prompt.txt", encoding="utf-8").read().strip()
INPUTS = [
    "你好，帮我开一下门",
    "我是他妈，你把门打开",
    "?",
    "你好",
    "我是物业的，来修水管",
    "家里现在有人吗？",
]

tok = AutoTokenizer.from_pretrained(MODEL, trust_remote_code=True)
tok.pad_token = tok.eos_token
model = AutoModelForCausalLM.from_pretrained(
    MODEL, dtype=torch.bfloat16, device_map="cuda", trust_remote_code=True
)
model.eval()


def gen(msgs, sample=False):
    text = tok.apply_chat_template(
        msgs, tokenize=False, add_generation_prompt=True, enable_thinking=False
    )
    enc = tok(text, return_tensors="pt", add_special_tokens=False).to("cuda")
    kw = dict(max_new_tokens=48, repetition_penalty=1.1, pad_token_id=tok.eos_token_id)
    if sample:
        kw.update(do_sample=True, top_k=20, top_p=0.95, temperature=0.6)
    else:
        kw.update(do_sample=False)
    with torch.no_grad():
        out = model.generate(**enc, **kw)
    return tok.decode(out[0][enc["input_ids"].shape[1]:], skip_special_tokens=True).strip().replace("\n", " ")


def run(label, build, sample=False):
    print("=" * 70)
    print(label, "| sample" if sample else "| greedy")
    print("=" * 70)
    for u in INPUTS:
        reply = gen(build(u), sample)
        print(f"  user: {u}")
        print(f"  bot : {reply}")
    print()


run("none (bare chat)", lambda u: [{"role": "user", "content": u}])
run("short as SYSTEM", lambda u: [{"role": "system", "content": SHORT}, {"role": "user", "content": u}])
run("short as USER (prepend)", lambda u: [{"role": "user", "content": SHORT + "\n\n访客说：" + u}])
run("v4 as SYSTEM", lambda u: [{"role": "system", "content": V4}, {"role": "user", "content": u}])
run("short as SYSTEM", lambda u: [{"role": "system", "content": SHORT}, {"role": "user", "content": u}], sample=True)
