"""Agent 接口：列出显式注册的 Agent 及其输入输出结构。"""

from __future__ import annotations

from fastapi import APIRouter

from app.api.deps import Identity, Resources
from app.schemas.agents import AgentOut
from app.schemas.common import Page

router = APIRouter(tags=["agents"])


@router.get("/agents", response_model=Page[AgentOut])
async def agents(principal: Identity, container: Resources) -> dict:
    """返回显式注册项和原生 Schema；默认空列表准确表达尚未注册。"""
    principal.require("resources:read")
    return {"items": container.agents.describe()}
