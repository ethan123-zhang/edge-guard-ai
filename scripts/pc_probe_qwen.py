"""Probe Qwen3 tokenizer chat template on the PC (WSL) before building the harness."""
from transformers import AutoTokenizer

MODEL = "/home/ethan/sentinel-models/Qwen3-0.6B"
tok = AutoTokenizer.from_pretrained(MODEL, trust_remote_code=True)

print("=== chat_template head ===")
print((tok.chat_template or "")[:600])
print("=== apply, enable_thinking=False ===")
msgs = [{"role": "user", "content": "你好"}]
for kw in ({"enable_thinking": False}, {}):
    try:
        s = tok.apply_chat_template(
            msgs, tokenize=False, add_generation_prompt=True, **kw
        )
        print("KW", kw, "OK")
        print(repr(s))
    except Exception as e:  # noqa: BLE001
        print("KW", kw, "ERR", type(e).__name__, e)
