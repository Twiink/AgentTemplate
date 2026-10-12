"""运行接口：查询运行与结果、取消运行、通过 SSE 回放运行事件。提交后台运行由 background 模块提供。"""

from __future__ import annotations

import asyncio
import json
from typing import Annotated

from fastapi import APIRouter, Header, Query, Request
from fastapi.responses import StreamingResponse

from app.api.deps import Identity, Limit, Offset, Resources
from app.core.errors import AppError
from app.db.models import Run
from app.schemas.common import Page
from app.schemas.runs import RunOut, RunResult

router = APIRouter(tags=["runs"])


@router.get("/runs", response_model=Page[RunOut])
async def list_runs(
    principal: Identity, container: Resources, limit: Limit = 50, offset: Offset = 0
) -> dict:
    """分页查询运行状态。"""
    principal.require("resources:read")
    return {"items": await container.repository.list(Run, principal, limit, offset)}


@router.get("/runs/{run_id}", response_model=RunOut)
async def get_run(run_id: str, principal: Identity, container: Resources) -> dict:
    """查询状态、用量、等待信息及结果。"""
    principal.require("resources:read")
    return await container.repository.get(Run, run_id, principal)


@router.get("/runs/{run_id}/result", response_model=RunResult)
async def get_result(run_id: str, principal: Identity, container: Resources) -> dict:
    """仅终态可取结果；其他状态返回明确冲突，避免空结果被认为成功。"""
    principal.require("resources:read")
    row = await container.repository.get(Run, run_id, principal)
    if row["status"] not in {"succeeded", "failed", "cancelled"}:
        raise AppError("run_not_finished", "运行尚未结束", 409)
    return {
        "status": row["status"],
        "output": row["output"],
        "error": row["error"],
        "usage": row["usage"],
    }


@router.post("/runs/{run_id}/cancel", response_model=RunOut)
async def cancel_run(run_id: str, principal: Identity, container: Resources) -> dict:
    """持久提交取消请求，由 Worker 协作停止。"""
    principal.require("resources:write")
    return await container.runs.cancel(principal, run_id)


@router.get("/runs/{run_id}/events")
async def run_events(
    run_id: str,
    request: Request,
    principal: Identity,
    container: Resources,
    last_event_id: Annotated[str | None, Header()] = None,
    after: Annotated[int, Query(ge=0)] = 0,
) -> StreamingResponse:
    """SSE 从 PostgreSQL 回放；心跳保持连接，客户端断开只结束订阅。"""
    principal.require("resources:read")
    await container.repository.get(Run, run_id, principal)
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
            events = await container.runs.events(principal, run_id, cursor)
            for event in events:
                cursor = event["sequence"]
                data = json.dumps(event, ensure_ascii=False)
                yield f"id: {cursor}\nevent: {event['kind']}\ndata: {data}\n\n"
            state = await container.repository.get(Run, run_id, principal)
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
