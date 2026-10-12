"""Agent 注册信息的响应模型。"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel


class AgentOut(BaseModel):
    """自动发现的 Agent；输入结构为声明时给出的 JSON Schema，缺省表示 messages 输入。"""

    id: str
    description: str
    input_schema: dict[str, Any] | None = None
