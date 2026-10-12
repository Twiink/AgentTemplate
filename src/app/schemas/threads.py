"""线程接口的请求与响应模型。"""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, Field


class ThreadInput(BaseModel):
    """线程名称仅用于展示，不承担身份绑定。"""

    title: str | None = Field(default=None, max_length=255)


class ThreadOut(BaseModel):
    """线程归属于当前主体；active_run 为正在占用线程的运行 ID。"""

    id: str
    created_at: datetime
    title: str | None = None
    active_run: str | None = None
