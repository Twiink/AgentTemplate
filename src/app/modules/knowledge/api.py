"""知识库接口：创建与查询知识库、导入文档、查看导入进度、检索、重建索引与删除。"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends
from fastapi.responses import Response

from app.api.deps import Identity, Limit, Offset, Resources
from app.core.errors import AppError
from app.modules.knowledge.models import Document, ImportJob, KnowledgeBase
from app.modules.knowledge.schemas import (
    DocumentOut,
    ImportInput,
    ImportJobOut,
    KnowledgeBaseOut,
    KnowledgeIndexOut,
    KnowledgeInput,
    RebuildInput,
    SearchHit,
    SearchInput,
)
from app.modules.knowledge.service import KnowledgeService
from app.schemas.common import Page

router = APIRouter(tags=["knowledge"])


def get_knowledge(container: Resources) -> KnowledgeService:
    """取得知识服务；路由只在 knowledge 模块启用时注册，服务缺失说明装配异常。"""
    if container.knowledge is None:
        raise AppError("module_unavailable", "knowledge 模块未装配", 503)
    return container.knowledge


Knowledge = Annotated[KnowledgeService, Depends(get_knowledge)]


@router.post("/knowledge", status_code=201, response_model=KnowledgeBaseOut)
async def create_knowledge(body: KnowledgeInput, principal: Identity, knowledge: Knowledge) -> dict:
    """创建独立向量空间的知识库。"""
    principal.require("resources:write")
    return await knowledge.create(principal, **body.model_dump())


@router.get("/knowledge", response_model=Page[KnowledgeBaseOut])
async def list_knowledge(
    principal: Identity, container: Resources, limit: Limit = 50, offset: Offset = 0
) -> dict:
    """列举可访问知识库。"""
    principal.require("resources:read")
    return {"items": await container.repository.list(KnowledgeBase, principal, limit, offset)}


@router.get("/knowledge/{knowledge_id}", response_model=KnowledgeBaseOut)
async def knowledge_detail(knowledge_id: str, principal: Identity, container: Resources) -> dict:
    """读取知识库活动版本。"""
    principal.require("resources:read")
    return await container.repository.get(KnowledgeBase, knowledge_id, principal)


@router.post("/knowledge/{knowledge_id}/imports", status_code=202, response_model=ImportJobOut)
async def import_document(
    knowledge_id: str, body: ImportInput, principal: Identity, knowledge: Knowledge
) -> dict:
    """持久记录真实导入任务，Worker 执行解析与向量化。"""
    principal.require("resources:write")
    return await knowledge.submit(principal, knowledge_id, **body.model_dump())


@router.get("/knowledge/{knowledge_id}/documents", response_model=Page[DocumentOut])
async def documents(
    knowledge_id: str,
    principal: Identity,
    container: Resources,
    limit: Limit = 50,
    offset: Offset = 0,
) -> dict:
    """列举知识库文档，不暴露其他主体来源。"""
    principal.require("resources:read")
    await container.repository.get(KnowledgeBase, knowledge_id, principal)
    filters = [Document.knowledge_id == knowledge_id]
    return {"items": await container.repository.list(Document, principal, limit, offset, filters)}


@router.get("/knowledge/{knowledge_id}/imports/{job_id}", response_model=ImportJobOut)
async def import_status(
    knowledge_id: str, job_id: str, principal: Identity, container: Resources
) -> dict:
    """查询进度与准确失败分类。"""
    principal.require("resources:read")
    row = await container.repository.get(ImportJob, job_id, principal)
    if row["knowledge_id"] != knowledge_id:
        raise AppError("resource_not_found", "导入任务不存在", 404)
    return row


@router.post("/knowledge/{knowledge_id}/imports/{job_id}/{action}", response_model=ImportJobOut)
async def import_action(
    knowledge_id: str, job_id: str, action: str, principal: Identity, knowledge: Knowledge
) -> dict:
    """取消或重试真实导入任务；仅失败与取消状态允许重试。"""
    principal.require("resources:write")
    return await knowledge.import_action(principal, knowledge_id, job_id, action)


@router.post("/knowledge/{knowledge_id}/search", response_model=Page[SearchHit])
async def search_knowledge(
    knowledge_id: str, body: SearchInput, principal: Identity, knowledge: Knowledge
) -> dict:
    """执行真实向量检索与可选重排。"""
    principal.require("resources:read")
    return {"items": await knowledge.search(principal, knowledge_id, **body.model_dump())}


@router.post("/knowledge/{knowledge_id}/rebuild", status_code=202, response_model=KnowledgeIndexOut)
async def rebuild(
    knowledge_id: str, body: RebuildInput, principal: Identity, knowledge: Knowledge
) -> dict:
    """建立新向量空间，任务全部完成后再发布。"""
    principal.require("resources:write")
    return await knowledge.rebuild(principal, knowledge_id, body.embedding)


@router.delete("/knowledge/{knowledge_id}/documents/{document_id}", status_code=204)
async def delete_document(
    knowledge_id: str,
    document_id: str,
    principal: Identity,
    container: Resources,
    knowledge: Knowledge,
) -> Response:
    """验证文档属于所选知识库后删除全部版本片段。"""
    principal.require("resources:write")
    row = await container.repository.get(Document, document_id, principal)
    if row["knowledge_id"] != knowledge_id:
        raise AppError("resource_not_found", "文档不存在", 404)
    await knowledge.delete_document(principal, document_id)
    return Response(status_code=204)


@router.delete("/knowledge/{knowledge_id}", status_code=204)
async def delete_knowledge(
    knowledge_id: str, principal: Identity, knowledge: Knowledge
) -> Response:
    """删除空闲知识库及其版本片段，原文件按附件生命周期保留。"""
    principal.require("resources:write")
    await knowledge.delete_knowledge(principal, knowledge_id)
    return Response(status_code=204)
