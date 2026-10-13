"""长期记忆接口：按命名空间与键写入、分页或关键词查询、语义检索、读取与删除。"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends
from fastapi.responses import Response

from app.api.deps import Identity, Limit, Offset, Resources
from app.core.errors import AppError, Code
from app.modules.memory.models import Memory
from app.modules.memory.schemas import MemoryInput, MemoryOut, SemanticSearchInput
from app.modules.memory.service import MemoryService
from app.schemas.common import Page

router = APIRouter(tags=["memories"])


def get_memories(container: Resources) -> MemoryService:
    """取得记忆服务；路由只在 memory 模块启用时注册，服务缺失说明装配异常。"""
    if container.memories is None:
        raise AppError(Code.MODULE_UNAVAILABLE, "memory 模块未装配", 503)
    return container.memories


Memories = Annotated[MemoryService, Depends(get_memories)]


@router.put("/memories", response_model=MemoryOut)
async def put_memory(body: MemoryInput, principal: Identity, memories: Memories) -> dict:
    """显式创建或更新长期记忆，同键更新保留版本。"""
    principal.require("resources:write")
    return await memories.put(principal, **body.model_dump())


@router.get("/memories", response_model=Page[MemoryOut])
async def list_memories(
    principal: Identity,
    container: Resources,
    memories: Memories,
    limit: Limit = 50,
    offset: Offset = 0,
    namespace: str | None = None,
    query: str | None = None,
) -> dict:
    """分页或关键词搜索；语义检索使用专用接口。"""
    principal.require("resources:read")
    if query:
        return {"items": await memories.search(principal, query, namespace, limit)}
    filters = [Memory.namespace == namespace] if namespace else []
    return {"items": await container.repository.list(Memory, principal, limit, offset, filters)}


@router.post("/memories/semantic-search", response_model=Page[MemoryOut])
async def semantic_search(
    body: SemanticSearchInput, principal: Identity, memories: Memories
) -> dict:
    """语义检索：使用 embedding 向量相似度查找相关记忆。"""
    principal.require("resources:read")
    return {"items": await memories.semantic_search(principal, **body.model_dump())}


@router.get("/memories/namespaces", response_model=Page[dict])
async def list_namespaces(principal: Identity, memories: Memories, limit: Limit = 100) -> dict:
    """列出当前用户的所有命名空间。"""
    principal.require("resources:read")
    return {"items": await memories.list_namespaces(principal, limit)}


@router.get("/memories/{memory_id}", response_model=MemoryOut)
async def get_memory(memory_id: str, principal: Identity, memories: Memories) -> dict:
    """读取长期记忆及来源。"""
    principal.require("resources:read")
    return await memories.get(principal, memory_id.split("/")[-2], memory_id.split("/")[-1])


@router.delete("/memories/{memory_id}", status_code=204)
async def delete_memory(memory_id: str, principal: Identity, memories: Memories) -> Response:
    """永久删除一条显式记忆。"""
    principal.require("resources:write")
    await memories.delete(principal, memory_id)
    return Response(status_code=204)
