"""Minimal sanity check: is the downloaded Qwen3-0.6B actually instruct-tuned,
and does the chat template put us in the assistant role?"""
import torch
from transformers import AutoModelForCausalLM, AutoTokenizer

MODEL = "/home/ethan/sentinel-models/Qwen3-0.6B"
tok = AutoTokenizer.from_pretrained(MODEL, trust_remote_code=True)
tok.pad_token = tok.eos_token
model = AutoModelForCausalLM.from_pretrained(
    MODEL, dtype=torch.bfloat16, device_map="cuda", trust_remote_code=True
)
model.eval()

def run(label, msgs=None, raw=None, thinking=False, n=32):
    if raw is not None:
        text = raw
    else:
        text = tok.apply_chat_template(
            msgs, tokenize=False, add_generation_prompt=True, enable_thinking=thinking
        )
    print("=" * 60)
    print(label)
    print("PROMPT:", repr(text[-160:]))
    enc = tok(text, return_tensors="pt", add_special_tokens=False).to("cuda")
    with torch.no_grad():
        out = model.generate(
            **enc,
            max_new_tokens=n,
            do_sample=False,
            repetition_penalty=1.1,
            pad_token_id=tok.eos_token_id,
        )
    new = out[0][enc["input_ids"].shape[1]:]
    print("REPLY:", repr(tok.decode(new, skip_special_tokens=True)))
    print()

run("A. chat, 1+1", msgs=[{"role": "user", "content": "1+1等于几？"}])
run("B. chat, 你好", msgs=[{"role": "user", "content": "你好"}])
run("C. chat, 你好 (thinking on)", msgs=[{"role": "user", "content": "你好"}], thinking=True)
run("D. raw continuation", raw="1+1等于几？答：")
run("E. chat with system", msgs=[
    {"role": "system", "content": "你是一个门口迎宾助手，只说一两句话。"},
    {"role": "user", "content": "你好"},
])
