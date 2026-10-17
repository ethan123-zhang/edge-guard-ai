"""Two remaining questions for Path A:
1) Can the 0.6B produce an acceptable 'acknowledge the purpose' line?
2) Does the narrow canned prompt still work when injected as a USER message
   (the only role the board's RKLLM input supports)?
"""
import torch
from transformers import AutoModelForCausalLM, AutoTokenizer

MODEL = "/home/ethan/sentinel-models/Qwen3-0.6B"
PURPOSE = ["我是快递，有个包裹要送", "张先生约我来吃饭", "我来取个件"]
NEUTRAL = ["你好", "?"]

ASK_SYS = "输出必须是：您好，请问您有什么事？"
ACK_SYS = "访客已说明来意。输出必须是：好的，我记下了，会转告给住户的。"

tok = AutoTokenizer.from_pretrained(MODEL, trust_remote_code=True)
tok.pad_token = tok.eos_token
model = AutoModelForCausalLM.from_pretrained(
    MODEL, dtype=torch.bfloat16, device_map="cuda", trust_remote_code=True
)
model.eval()


def gen(msgs, n=24):
    text = tok.apply_chat_template(msgs, tokenize=False, add_generation_prompt=True, enable_thinking=False)
    enc = tok(text, return_tensors="pt", add_special_tokens=False).to("cuda")
    with torch.no_grad():
        out = model.generate(**enc, max_new_tokens=n, do_sample=False,
                             repetition_penalty=1.1, pad_token_id=tok.eos_token_id)
    return tok.decode(out[0][enc["input_ids"].shape[1]:], skip_special_tokens=True).strip().replace("\n", " ")


print("### 1. ACK as SYSTEM")
for u in PURPOSE:
    print(" ", u, "->", gen([{"role": "system", "content": ACK_SYS}, {"role": "user", "content": u}]))

print("\n### 2. ASK as SYSTEM")
for u in NEUTRAL:
    print(" ", u, "->", gen([{"role": "system", "content": ASK_SYS}, {"role": "user", "content": u}]))

print("\n### 3. ASK as separate USER turn + dummy assistant (board today)")
for u in NEUTRAL:
    msgs = [{"role": "user", "content": ASK_SYS}, {"role": "assistant", "content": "好的。"},
            {"role": "user", "content": u}]
    print(" ", u, "->", gen(msgs))

print("\n### 4. ASK prepended to the first user turn")
for u in NEUTRAL:
    msgs = [{"role": "user", "content": ASK_SYS + "\n\n访客说：" + u}]
    print(" ", u, "->", gen(msgs))
