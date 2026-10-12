"""Agent 接口：列出启动时自动发现并构建的 Agent 及其输入结构。"""

from __future__ import annotations

from fastapi import APIRouter

from app.api.deps import Identity, Resources
from app.schemas.agents import AgentOut
from app.schemas.common import Page

router = APIRouter(tags=["agents"])


@router.get("/agents", response_model=Page[AgentOut])
async def agents(principal: Identity, container: Resources) -> dict:
    """返回自动发现的 Agent；未声明 input_schema 时按 messages 输入处理。"""
    principal.require("resources:read")
    return {"items": container.agents.describe()}
