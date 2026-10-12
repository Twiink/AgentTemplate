"""Agent 注册信息的响应模型。"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel


class AgentOut(BaseModel):
    """显式注册的 Agent；输入输出结构为注册时声明的 JSON Schema。"""

    id: str
    framework: str
    input_schema: dict[str, Any]
    output_schema: dict[str, Any] | None = None
    capabilities: list[str]
