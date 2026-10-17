"""Can the 0.6B be constrained to ONE canned question? If yes, the LLM can
still own the 'ask purpose' turn without free-form risk."""
import torch
from transformers import AutoModelForCausalLM, AutoTokenizer

MODEL = "/home/ethan/sentinel-models/Qwen3-0.6B"
INPUTS = ["你好", "?", "今天天气不错", "我找人"]

SYS = [
    "你只问访客一句话：您有什么事？不要说明的，不要问候。",
    "输出必须是：您好，请问您有什么事？",
    "访客来了。你问：您有什么事？",
]

tok = AutoTokenizer.from_pretrained(MODEL, trust_remote_code=True)
tok.pad_token = tok.eos_token
model = AutoModelForCausalLM.from_pretrained(
    MODEL, dtype=torch.bfloat16, device_map="cuda", trust_remote_code=True
)
model.eval()

for s in SYS:
    print("=" * 70)
    print("SYS:", s)
    for u in INPUTS:
        msgs = [{"role": "system", "content": s}, {"role": "user", "content": u}]
        text = tok.apply_chat_template(msgs, tokenize=False, add_generation_prompt=True, enable_thinking=False)
        enc = tok(text, return_tensors="pt", add_special_tokens=False).to("cuda")
        with torch.no_grad():
            out = model.generate(**enc, max_new_tokens=24, do_sample=False,
                                 repetition_penalty=1.1, pad_token_id=tok.eos_token_id)
        r = tok.decode(out[0][enc["input_ids"].shape[1]:], skip_special_tokens=True).strip().replace("\n", " ")
        print(f"  {u!r} -> {r!r}")
