"""运行接口的请求与响应模型；运行配置、快照、恢复输入与租约等内部字段不会出现在响应中。"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field


class RunInput(BaseModel):
    """运行输入由注册 Schema 再次验证；原生配置在各框架命名空间下。"""

    agent_id: str
    thread_id: str
    input: dict[str, Any]
    options: dict[str, Any] = Field(default_factory=dict)


class HumanInput(BaseModel):
    """人工响应绑定等待事件序号，防止重复恢复和过期页面误操作。"""

    expected_sequence: int = Field(ge=1)
    response: dict[str, Any]


class RunOut(BaseModel):
    """运行状态、用量、等待信息与结果。"""

    id: str
    created_at: datetime
    thread_id: str
    agent_id: str
    status: str
    input: dict[str, Any]
    output: Any = None
    error: dict[str, Any] | None = None
    pending: dict[str, Any] | None = None
    usage: dict[str, Any] = Field(default_factory=dict)
    prompt_versions: dict[str, str] = Field(default_factory=dict)
    cancel_requested: bool = False
    event_sequence: int = 0
    completed_at: datetime | None = None


class RunResult(BaseModel):
    """终态运行的结果；尚未结束的运行不返回此结构。"""

    status: str
    output: Any = None
    error: dict[str, Any] | None = None
    usage: dict[str, Any] = Field(default_factory=dict)
