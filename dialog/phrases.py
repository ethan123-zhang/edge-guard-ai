"""固定话术库（docs/03 §5.4、§6；验收项 M6）。

这些句子必须写死在代码里，理由来自 2026-10-17 的板端实测：
提示词为了教会模型"什么时候说转达"，从 1425 B 涨到 3022 B，还在反复横跳
（讲"主动转达"就第一句就转达，加限定条件就迟到一轮）。

**流程的收尾标志必须是确定的**：什么时候说话归状态机管，话怎么说才归 LLM 管。
"""
from __future__ import annotations

from typing import Optional

# §5.4 转达播报语句 —— 一字不改
HANDOFF_TEMPLATE = "好的，已经帮您转告家里人了，{称呼}再见。"
# 拿不到称呼时的版本
HANDOFF_NO_NAME = "好的，已经帮您转告家里人了，再见。"

# §6 异常与边界处理的固定播报
NOT_HEARD = "不好意思，我没听清，您再说一遍？"
SILENCE_EXIT = "您要是不方便说，我先记下您来过。"
LLM_TIMEOUT = "您稍等，我先记一下。"

# §5.2 陌生人 / 熟人问候
GREETING_UNKNOWN = "您好，家里现在没人，请问您找哪位？"
GREETING_KNOWN = "您好，{name}，又见面了。今天来是有什么事吗？"


def handoff(caller_name: Optional[str] = None) -> str:
    """收尾播报。**这是流程结束的唯一标志，不许 LLM 代劳。**"""
    name = (caller_name or "").strip()
    if not name:
        return HANDOFF_NO_NAME
    return HANDOFF_TEMPLATE.format(称呼=name)


def greeting(name: Optional[str] = None, visit_count: int = 0) -> str:
    """开场问候。熟人带历史上下文（核心亮点 ①）。"""
    name = (name or "").strip()
    if not name:
        return GREETING_UNKNOWN
    if visit_count > 1:
        return f"您好，{name}，您这是第 {visit_count} 次来了。今天有什么事吗？"
    return GREETING_KNOWN.format(name=name)
