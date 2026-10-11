"""公共 API 协议层；服务负责状态与存储，所有资源请求复用可信身份及统一错误。"""

from __future__ import annotations

import asyncio
import json
from typing import Annotated, Any
from urllib.parse import quote

from fastapi import APIRouter, Depends, Header, Query, Request, UploadFile
from fastapi.responses import Response, StreamingResponse
from pydantic import BaseModel, Field
from sqlalchemy import select

from app.core.errors import AppError
from app.core.security import Principal, current_principal
from app.db.models import Artifact, Document, ImportJob, KnowledgeBase, Memory, Outbox, Run, Thread
from app.services.data import attributes, owned, serialize

router = APIRouter(prefix="/api/v1")
Identity = Annotated[Principal, Depends(current_principal)]
Limit = Annotated[int, Query(ge=1, le=100)]
Offset = Annotated[int, Query(ge=0)]


class ThreadInput(BaseModel):
    """线程名称仅用于展示，不承担身份绑定。"""

    title: str | None = Field(default=None, max_length=255)


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


class MemoryInput(BaseModel):
    """显式长期记忆输入；来源由业务提供，模板保留追踪信息。"""

    namespace: str = Field(min_length=1, max_length=255)
    key: str = Field(min_length=1, max_length=255)
    content: str = Field(min_length=1, max_length=100000)
    sources: list[dict[str, Any]] = Field(default_factory=list)
    metadata: dict[str, Any] = Field(default_factory=dict)


class KnowledgeInput(BaseModel):
    """知识库创建需明确选择独立 Embedding 配置。"""

    name: str = Field(min_length=1, max_length=255)
    embedding: str


class ImportInput(BaseModel):
    """附件输入与切分参数；同名文档关联新版本。"""

    artifact_id: str
    name: str = Field(min_length=1, max_length=255)
    chunk_size: int = Field(default=1200, ge=100, le=10000)
    overlap: int = Field(default=150, ge=0)


class SearchInput(BaseModel):
    """检索参数不包含身份，过滤条件只能缩小当前主体的结果范围。"""

    query: str = Field(min_length=1, max_length=20000)
    top_k: int = Field(default=5, ge=1, le=50)
    metadata: dict[str, Any] = Field(default_factory=dict)
    rerank: bool = False


class RebuildInput(BaseModel):
    """索引重建目标配置名称。"""

    embedding: str


@router.get("/agents")
async def agents(request: Request, principal: Identity) -> dict:
    """返回显式注册项和原生 Schema；默认空列表准确表达尚未注册。"""
    principal.require("resources:read")
    return {"items": request.app.state.resources.agents.describe()}


@router.post("/threads", status_code=201)
async def create_thread(body: ThreadInput, request: Request, principal: Identity) -> dict:
    """创建归属于当前主体的线程。"""
    principal.require("resources:write")
    return await request.app.state.resources.data.create_thread(principal, body.title)


@router.get("/threads")
async def threads(
    request: Request, principal: Identity, limit: Limit = 50, offset: Offset = 0
) -> dict:
    """分页列举可访问线程。"""
    principal.require("resources:read")
    return {"items": await request.app.state.resources.data.list(Thread, principal, limit, offset)}


@router.get("/threads/{thread_id}")
async def thread(thread_id: str, request: Request, principal: Identity) -> dict:
    """查询线程归属和活动运行引用。"""
    principal.require("resources:read")
    return await request.app.state.resources.data.get(Thread, thread_id, principal)


@router.delete("/threads/{thread_id}", status_code=204)
async def delete_thread(thread_id: str, request: Request, principal: Identity) -> Response:
    """删除空闲线程及原生状态，活动运行须先终止。"""
    principal.require("resources:write")
    await request.app.state.resources.data.delete_thread(principal, thread_id)
    return Response(status_code=204)


@router.post("/runs", status_code=202)
async def create_run(body: RunInput, request: Request, principal: Identity) -> dict:
    """持久排队并返回运行 ID；请求退出和连接断开不会影响 Worker。"""
    principal.require("resources:write")
    return await request.app.state.resources.runs.submit(principal, **body.model_dump())


@router.get("/runs")
async def list_runs(
    request: Request, principal: Identity, limit: Limit = 50, offset: Offset = 0
) -> dict:
    """分页查询运行状态。"""
    principal.require("resources:read")
    return {"items": await request.app.state.resources.data.list(Run, principal, limit, offset)}


@router.get("/runs/{run_id}")
async def get_run(run_id: str, request: Request, principal: Identity) -> dict:
    """查询状态、用量、等待信息及结果。"""
    principal.require("resources:read")
    return await request.app.state.resources.data.get(Run, run_id, principal)


@router.get("/runs/{run_id}/result")
async def get_result(run_id: str, request: Request, principal: Identity) -> dict:
    """仅终态可取结果；其他状态返回明确冲突，避免空结果被认为成功。"""
    principal.require("resources:read")
    row = await request.app.state.resources.data.get(Run, run_id, principal)
    if row["status"] not in {"succeeded", "failed", "cancelled"}:
        raise AppError("run_not_finished", "运行尚未结束", 409)
    return {
        "status": row["status"],
        "output": row["output"],
        "error": row["error"],
        "usage": row["usage"],
    }


@router.post("/runs/{run_id}/cancel")
async def cancel_run(run_id: str, request: Request, principal: Identity) -> dict:
    """持久提交取消请求，由 Worker 协作停止。"""
    principal.require("resources:write")
    return await request.app.state.resources.runs.cancel(principal, run_id)


@router.post("/runs/{run_id}/responses", status_code=202)
async def human_response(
    run_id: str, body: HumanInput, request: Request, principal: Identity
) -> dict:
    """消费当前等待输入，恢复任务通过 outbox 投递。"""
    principal.require("resources:write")
    return await request.app.state.resources.runs.respond(
        principal, run_id, body.response, body.expected_sequence
    )


@router.get("/runs/{run_id}/events")
async def run_events(
    run_id: str,
    request: Request,
    principal: Identity,
    last_event_id: Annotated[str | None, Header()] = None,
    after: Annotated[int, Query(ge=0)] = 0,
) -> StreamingResponse:
    """SSE 从 PostgreSQL 回放；心跳保持连接，客户端断开只结束订阅。"""
    principal.require("resources:read")
    resources = request.app.state.resources
    await resources.data.get(Run, run_id, principal)
    try:
        sequence = max(after, int(last_event_id or 0))
    except ValueError as error:
        raise AppError("invalid_event_id", "Last-Event-ID 必须为非负序号", 422) from error
    if sequence < 0:
        raise AppError("invalid_event_id", "事件序号无效", 422)

    async def stream():
        """单行 JSON 防止正文换行破坏 SSE 格式；终态在全部回放后关闭订阅。"""
        cursor = sequence
        heartbeat = 0
        while not await request.is_disconnected():
            events = await resources.runs.events(principal, run_id, cursor)
            for event in events:
                cursor = event["sequence"]
                yield f"id: {cursor}\nevent: {event['kind']}\ndata: {json.dumps(event, ensure_ascii=False)}\n\n"
            state = await resources.data.get(Run, run_id, principal)
            if (
                state["status"] in {"succeeded", "failed", "cancelled"}
                and cursor >= state["event_sequence"]
            ):
                return
            heartbeat += 1
            if heartbeat >= 15:
                yield ": 心跳\n\n"
                heartbeat = 0
            await asyncio.sleep(1)

    return StreamingResponse(
        stream(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@router.put("/memories")
async def put_memory(body: MemoryInput, request: Request, principal: Identity) -> dict:
    """显式创建或更新长期记忆，同键更新保留版本。"""
    principal.require("resources:write")
    return await request.app.state.resources.data.put_memory(principal, **body.model_dump())


@router.get("/memories")
async def memories(
    request: Request,
    principal: Identity,
    limit: Limit = 50,
    offset: Offset = 0,
    namespace: str | None = None,
    query: str | None = None,
) -> dict:
    """分页或关键词搜索；语义检索由独立知识服务提供。"""
    principal.require("resources:read")
    data = request.app.state.resources.data
    if query:
        return {"items": await data.search_memory(principal, query, namespace, limit)}
    filters = [Memory.namespace == namespace] if namespace else []
    return {"items": await data.list(Memory, principal, limit, offset, filters)}


@router.get("/memories/{memory_id}")
async def memory(memory_id: str, request: Request, principal: Identity) -> dict:
    """读取长期记忆及来源。"""
    principal.require("resources:read")
    return await request.app.state.resources.data.get(Memory, memory_id, principal)


@router.delete("/memories/{memory_id}", status_code=204)
async def delete_memory(memory_id: str, request: Request, principal: Identity) -> Response:
    """永久删除一条显式记忆。"""
    principal.require("resources:write")
    await request.app.state.resources.data.delete_memory(principal, memory_id)
    return Response(status_code=204)


@router.post("/artifacts", status_code=201)
async def upload_artifact(request: Request, principal: Identity, file: UploadFile) -> dict:
    """有限读取上传内容，超限时中止，不把用户文件名用作对象路径。"""
    principal.require("resources:write")
    resources = request.app.state.resources
    content = await file.read(resources.settings.storage.max_bytes + 1)
    await file.close()
    return await resources.data.upload(
        principal, file.filename or "附件", file.content_type or "application/octet-stream", content
    )


@router.get("/artifacts")
async def artifacts(
    request: Request, principal: Identity, limit: Limit = 50, offset: Offset = 0
) -> dict:
    """列举当前主体附件元数据。"""
    principal.require("resources:read")
    return {
        "items": await request.app.state.resources.data.list(Artifact, principal, limit, offset)
    }


@router.get("/artifacts/{artifact_id}")
async def artifact(artifact_id: str, request: Request, principal: Identity) -> Response:
    """校验归属后读取真实内容，文件名通过 RFC 编码写入下载头。"""
    principal.require("resources:read")
    metadata, content = await request.app.state.resources.data.download(principal, artifact_id)
    return Response(
        content,
        media_type=metadata["content_type"],
        headers={
            "Content-Disposition": "attachment; filename*=UTF-8''"
            + quote(metadata["filename"], safe="")
        },
    )


@router.post("/knowledge", status_code=201)
async def create_knowledge(body: KnowledgeInput, request: Request, principal: Identity) -> dict:
    """创建独立向量空间的知识库。"""
    principal.require("resources:write")
    return await request.app.state.resources.knowledge.create(principal, **body.model_dump())


@router.get("/knowledge")
async def knowledge(
    request: Request, principal: Identity, limit: Limit = 50, offset: Offset = 0
) -> dict:
    """列举可访问知识库。"""
    principal.require("resources:read")
    return {
        "items": await request.app.state.resources.data.list(
            KnowledgeBase, principal, limit, offset
        )
    }


@router.get("/knowledge/{knowledge_id}")
async def knowledge_detail(knowledge_id: str, request: Request, principal: Identity) -> dict:
    """读取知识库活动版本。"""
    principal.require("resources:read")
    return await request.app.state.resources.data.get(KnowledgeBase, knowledge_id, principal)


@router.post("/knowledge/{knowledge_id}/imports", status_code=202)
async def import_document(
    knowledge_id: str, body: ImportInput, request: Request, principal: Identity
) -> dict:
    """持久记录真实导入任务，Worker 执行解析与向量化。"""
    principal.require("resources:write")
    return await request.app.state.resources.knowledge.submit(
        principal, knowledge_id, **body.model_dump()
    )


@router.get("/knowledge/{knowledge_id}/documents")
async def documents(
    knowledge_id: str, request: Request, principal: Identity, limit: Limit = 50, offset: Offset = 0
) -> dict:
    """列举知识库文档，不暴露其他主体来源。"""
    principal.require("resources:read")
    resources = request.app.state.resources
    await resources.data.get(KnowledgeBase, knowledge_id, principal)
    return {
        "items": await resources.data.list(
            Document, principal, limit, offset, [Document.knowledge_id == knowledge_id]
        )
    }


@router.get("/knowledge/{knowledge_id}/imports/{job_id}")
async def import_status(
    knowledge_id: str, job_id: str, request: Request, principal: Identity
) -> dict:
    """查询进度与准确失败分类。"""
    principal.require("resources:read")
    row = await request.app.state.resources.data.get(ImportJob, job_id, principal)
    if row["knowledge_id"] != knowledge_id:
        raise AppError("resource_not_found", "导入任务不存在", 404)
    return row


@router.post("/knowledge/{knowledge_id}/imports/{job_id}/{action}")
async def import_action(
    knowledge_id: str, job_id: str, action: str, request: Request, principal: Identity
) -> dict:
    """取消或重试真实导入任务；仅失败与取消状态允许重试。"""
    principal.require("resources:write")
    async with request.app.state.resources.db.transaction(principal) as session:
        job = (
            await session.scalars(
                select(ImportJob)
                .where(
                    ImportJob.id == job_id,
                    ImportJob.knowledge_id == knowledge_id,
                    *owned(ImportJob, principal),
                )
                .with_for_update()
            )
        ).one_or_none()
        if job is None:
            raise AppError("resource_not_found", "导入任务不存在", 404)
        if action == "cancel":
            job.cancel_requested = True
            if job.status == "queued":
                job.status = "cancelled"
        elif action == "retry" and job.status in {"failed", "cancelled"}:
            job.status, job.cancel_requested, job.error = "queued", False, None
            session.add(Outbox(**attributes(principal), task="knowledge", resource_id=job.id))
        else:
            raise AppError("import_conflict", "导入状态或操作不允许", 409)
        return serialize(job)


@router.post("/knowledge/{knowledge_id}/search")
async def search_knowledge(
    knowledge_id: str, body: SearchInput, request: Request, principal: Identity
) -> dict:
    """执行真实向量检索与可选重排。"""
    principal.require("resources:read")
    return {
        "items": await request.app.state.resources.knowledge.search(
            principal, knowledge_id, **body.model_dump()
        )
    }


@router.post("/knowledge/{knowledge_id}/rebuild", status_code=202)
async def rebuild(
    knowledge_id: str, body: RebuildInput, request: Request, principal: Identity
) -> dict:
    """建立新向量空间，任务全部完成后再发布。"""
    principal.require("resources:write")
    return await request.app.state.resources.knowledge.rebuild(
        principal, knowledge_id, body.embedding
    )


@router.delete("/knowledge/{knowledge_id}/documents/{document_id}", status_code=204)
async def delete_document(
    knowledge_id: str, document_id: str, request: Request, principal: Identity
) -> Response:
    """验证文档属于所选知识库后删除全部版本片段。"""
    principal.require("resources:write")
    row = await request.app.state.resources.data.get(Document, document_id, principal)
    if row["knowledge_id"] != knowledge_id:
        raise AppError("resource_not_found", "文档不存在", 404)
    await request.app.state.resources.knowledge.delete_document(principal, document_id)
    return Response(status_code=204)


@router.get("/threads/{thread_id}/state")
async def thread_state(thread_id: str, request: Request, principal: Identity) -> dict:
    """读取已授权线程的原生状态，接口不能指定其他检查点命名空间。"""
    principal.require("resources:read")
    return await request.app.state.resources.data.thread_state(principal, thread_id)


@router.delete("/artifacts/{artifact_id}", status_code=204)
async def delete_artifact(artifact_id: str, request: Request, principal: Identity) -> Response:
    """删除未被知识任务引用的真实附件。"""
    principal.require("resources:write")
    await request.app.state.resources.data.delete_artifact(principal, artifact_id)
    return Response(status_code=204)


@router.delete("/knowledge/{knowledge_id}", status_code=204)
async def delete_knowledge(knowledge_id: str, request: Request, principal: Identity) -> Response:
    """删除空闲知识库及其版本片段，原文件按附件生命周期保留。"""
    principal.require("resources:write")
    await request.app.state.resources.knowledge.delete_knowledge(principal, knowledge_id)
    return Response(status_code=204)
