"""不联网的 token 估算；用于调用前的上下文与预算检查，真实用量以服务商返回的 usage 为准。"""

from __future__ import annotations

import json
import math
from collections.abc import Iterable
from typing import Any

# 中日韩字符在主流分词器中大多一到两个字对应一个 token，按每字一个 token 保守估算；
# 其余文本按约 4 个字符一个 token 估算。
CJK_RANGES = (
    (0x3000, 0x303F),
    (0x3040, 0x30FF),
    (0x3400, 0x4DBF),
    (0x4E00, 0x9FFF),
    (0xAC00, 0xD7AF),
    (0xF900, 0xFAFF),
    (0xFF00, 0xFFEF),
)
CHARACTERS_PER_TOKEN = 4
# 每条消息的角色、分隔符等结构开销。
MESSAGE_OVERHEAD = 4
# 图片、音频等非文本块无法按字符估算，按固定值计入，避免 base64 内容被当作文本放大。
NON_TEXT_BLOCK = 256


def is_cjk(character: str) -> bool:
    """判断字符是否属于中日韩文字或全角符号。"""
    code = ord(character)
    return any(start <= code <= end for start, end in CJK_RANGES)


def estimate_text(text: str) -> int:
    """估算一段文本的 token 数。"""
    if not text:
        return 0
    cjk = sum(1 for character in text if is_cjk(character))
    return cjk + math.ceil((len(text) - cjk) / CHARACTERS_PER_TOKEN)


def _content_tokens(content: Any) -> int:
    """估算消息内容；内容可以是字符串，也可以是 LangChain 的内容块列表。"""
    if isinstance(content, str):
        return estimate_text(content)
    if isinstance(content, list):
        total = 0
        for block in content:
            if isinstance(block, str):
                total += estimate_text(block)
            elif isinstance(block, dict) and isinstance(block.get("text"), str):
                total += estimate_text(block["text"])
            else:
                total += NON_TEXT_BLOCK
        return total
    return estimate_text(str(content)) if content is not None else 0


def estimate_messages(messages: Iterable[Any]) -> int:
    """估算一组消息的 token 数；支持 LangChain 消息对象和 role/content 字典。"""
    total = 0
    for message in messages:
        if isinstance(message, dict):
            content = message.get("content")
            tool_calls = message.get("tool_calls") or []
        else:
            content = getattr(message, "content", None)
            tool_calls = getattr(message, "tool_calls", None) or []
        total += MESSAGE_OVERHEAD + _content_tokens(content)
        for call in tool_calls:
            name = call.get("name", "") if isinstance(call, dict) else ""
            arguments = call.get("args", {}) if isinstance(call, dict) else {}
            total += estimate_text(name) + estimate_text(
                json.dumps(arguments, ensure_ascii=False, default=str)
            )
    return total
