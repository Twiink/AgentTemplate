"""长期记忆接口的请求与响应模型。"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field


class MemoryInput(BaseModel):
    """显式长期记忆输入；来源由业务提供，模板保留追踪信息。"""

    namespace: str = Field(min_length=1, max_length=255)
    key: str = Field(min_length=1, max_length=255)
    content: str = Field(min_length=1, max_length=100000)
    sources: list[dict[str, Any]] = Field(default_factory=list)
    metadata: dict[str, Any] = Field(default_factory=dict)


class SemanticSearchInput(BaseModel):
    """语义检索输入参数。"""

    query: str = Field(min_length=1, max_length=20000)
    namespace: str | None = None
    top_k: int = Field(default=10, ge=1, le=50)
    min_score: float = Field(default=0.7, ge=0.0, le=1.0)


class MemoryOut(BaseModel):
    """长期记忆及其来源；同键更新时 version 递增。"""

    id: str
    created_at: datetime
    namespace: str
    key: str
    content: str
    sources: list[dict[str, Any]] = Field(default_factory=list)
    metadata: dict[str, Any] = Field(default_factory=dict)
    version: int
    updated_at: datetime
    expires_at: datetime | None = None
    score: float | None = None
