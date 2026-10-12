"""线程接口：创建、列举、读取、删除线程，以及读取线程的原生状态。"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter
from fastapi.responses import Response

from app.api.deps import Identity, Limit, Offset, Resources
from app.db.models import Thread
from app.schemas.common import Page
from app.schemas.threads import ThreadInput, ThreadOut

router = APIRouter(tags=["threads"])


@router.post("/threads", status_code=201, response_model=ThreadOut)
async def create_thread(body: ThreadInput, principal: Identity, container: Resources) -> dict:
    """创建归属于当前主体的线程。"""
    principal.require("resources:write")
    return await container.threads.create(principal, body.title)


@router.get("/threads", response_model=Page[ThreadOut])
async def threads(
    principal: Identity, container: Resources, limit: Limit = 50, offset: Offset = 0
) -> dict:
    """分页列举可访问线程。"""
    principal.require("resources:read")
    return {"items": await container.repository.list(Thread, principal, limit, offset)}


@router.get("/threads/{thread_id}", response_model=ThreadOut)
async def thread(thread_id: str, principal: Identity, container: Resources) -> dict:
    """查询线程归属和活动运行引用。"""
    principal.require("resources:read")
    return await container.repository.get(Thread, thread_id, principal)


@router.delete("/threads/{thread_id}", status_code=204)
async def delete_thread(thread_id: str, principal: Identity, container: Resources) -> Response:
    """删除空闲线程及原生状态，活动运行须先终止。"""
    principal.require("resources:write")
    await container.threads.delete(principal, thread_id)
    return Response(status_code=204)


@router.get("/threads/{thread_id}/state")
async def thread_state(thread_id: str, principal: Identity, container: Resources) -> dict[str, Any]:
    """读取已授权线程的原生状态，接口不能指定其他检查点命名空间。"""
    principal.require("resources:read")
    return await container.threads.state(principal, thread_id)
