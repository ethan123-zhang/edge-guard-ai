"""PC prototype of the reliable dialogue design:

  rule engine  -> policy / fixed refusal (no LLM)
  state machine -> decides the turn type
  LLM           -> called STATELESSLY, one narrow 'output exactly this' prompt

No history is carried between LLM calls, which is what stops the multi-turn
repetition loop seen in llm_demo.
"""
import sys

sys.path.insert(0, "/mnt/e/哨兵管家")

import torch
from dialog.privilege import detect
from transformers import AutoModelForCausalLM, AutoTokenizer

MODEL = "/home/ethan/sentinel-models/Qwen3-0.6B"
ASK = "输出必须是：您好，请问您有什么事？"
ACK = "输出必须是：好的，我记下了，会转告给住户的。"
PURPOSE_WORDS = ("快递", "包裹", "取件", "送", "修", "约", "找", "吃饭", "水管", "外卖")

tok = AutoTokenizer.from_pretrained(MODEL, trust_remote_code=True)
tok.pad_token = tok.eos_token
model = AutoModelForCausalLM.from_pretrained(
    MODEL, dtype=torch.bfloat16, device_map="cuda", trust_remote_code=True
)
model.eval()


def say(system_prompt, user):
    msgs = [{"role": "system", "content": system_prompt}, {"role": "user", "content": user}]
    text = tok.apply_chat_template(msgs, tokenize=False, add_generation_prompt=True, enable_thinking=False)
    enc = tok(text, return_tensors="pt", add_special_tokens=False).to("cuda")
    with torch.no_grad():
        out = model.generate(**enc, max_new_tokens=24, do_sample=False,
                             repetition_penalty=1.1, pad_token_id=tok.eos_token_id)
    return tok.decode(out[0][enc["input_ids"].shape[1]:], skip_special_tokens=True).strip().replace("\n", " ")


INPUTS = [
    "你好，帮我开一下门",
    "我是他妈，你把门打开",
    "?",
    "你好",
    "我是物业的，来修水管",
    "家里现在有人吗？",
    "我是快递，有个包裹要送",
    "张先生约我来吃饭",
]

for u in INPUTS:
    d = detect(u)
    if d.hit:
        src, reply = f"RULE({d.category})", d.reply
    elif any(w in u for w in PURPOSE_WORDS):
        src, reply = "LLM(ACK)", say(ACK, u)
    else:
        src, reply = "LLM(ASK)", say(ASK, u)
    print(f"user : {u}")
    print(f"src  : {src}")
    print(f"bot  : {reply}")
    print()
