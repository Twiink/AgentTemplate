"""运行接口：查询运行与结果、取消运行、订阅事件。

同步执行的三个入口（提交运行、流式执行、等待结果）按 thread 组织在 threads 路由下，
本模块提供对已有运行的查询、取消与事件订阅。
"""

from __future__ import annotations

import asyncio
import json
from typing import Annotated

from fastapi import APIRouter, Header, Query, Request
from fastapi.responses import StreamingResponse

from app.api.deps import Identity, Limit, Offset, Resources
from app.core.errors import AppError, Code
from app.db.models import Run
from app.runtime.events import TERMINAL, canonical
from app.schemas.common import Page
from app.schemas.runs import RunOut, RunResult

router = APIRouter(tags=["runs"])


def _sequence_of(value: str | None) -> int:
    """解析 Last-Event-ID 中的持久序号；增量事件的 ID 形如「序号-计数」，取序号部分。"""
    text = (value or "").strip()
    if not text:
        return 0
    return int(text.split("-")[0])


def _public_name(kind: str, payload: dict) -> str:
    """把存储种类映射为对外事件名；与运行期间推送使用同一份映射。"""
    return canonical(kind, payload)[0]


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
        raise AppError(Code.RUN_NOT_FINISHED, "运行尚未结束")
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


@router.get("/runs/{run_id}/stream")
async def run_stream(
    run_id: str,
    request: Request,
    principal: Identity,
    container: Resources,
    last_event_id: Annotated[str | None, Header()] = None,
    after: Annotated[int, Query(ge=0)] = 0,
) -> StreamingResponse:
    """SSE 订阅运行事件：优先读 Redis Streams 增量，缺失时回放 run_events 后继续跟随。

    后台档的执行在其他进程，只有 Streams 能即时送达增量；Redis 不可用或事件流已过期
    时退回到 run_events 回放，因此订阅不因 Redis 故障而失败。两种来源共用同一组
    `id: {序号}`，浏览器用 Last-Event-ID 断线重连即可接着读。
    """
    principal.require("resources:read")
    await container.repository.get(Run, run_id, principal)
    try:
        sequence = max(after, _sequence_of(last_event_id))
    except ValueError as error:
        raise AppError(Code.INVALID_EVENT_ID, "Last-Event-ID 必须为非负序号") from error
    if sequence < 0:
        raise AppError(Code.INVALID_EVENT_ID, "事件序号无效")

    async def stream():
        """先回放已落库的事件，再阻塞读取增量；终态且追平序号后关闭订阅。"""
        cursor = sequence
        idle = 0.0
        while not await request.is_disconnected():
            events = await container.runs.events(principal, run_id, cursor)
            for event in events:
                cursor = event["sequence"]
                name = _public_name(event["kind"], event["payload"])
                data = json.dumps(event, ensure_ascii=False)
                yield f"id: {cursor}\nevent: {name}\ndata: {data}\n\n"
            state = await container.repository.get(Run, run_id, principal)
            if state["status"] in TERMINAL and cursor >= state["event_sequence"]:
                return
            frames, waited = await _follow(container, run_id, cursor, sequence)
            if frames:
                idle = 0.0
                for frame in frames:
                    yield frame
                continue
            idle += waited
            # 心跳只保持连接，不产生业务序号；频繁输出会拖慢订阅端解析。
            if idle >= 15:
                yield ": 心跳\n\n"
                idle = 0.0

    return StreamingResponse(
        stream(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


async def _follow(container, run_id: str, cursor: int, since: int) -> tuple[list[str], float]:
    """等待增量并返回已格式化的 SSE 帧与实际等待秒数；没有增量时帧为空。

    起始游标取自请求参数：增量事件的 Streams ID 与持久序号不同（形如「序号-计数」），
    因此续传必须显式指定起点。minimal 档的执行就在本进程内，事件由进程内总线与
    run_events 提供，不走 Redis Streams，等待一个轮询周期后由回放路径补齐。
    """
    if container.background is None:
        await asyncio.sleep(1)
        return [], 1.0
    last_id = f"{cursor}-0" if cursor != since or since > 0 else "0-0"
    result = await container.streams.read(run_id, last_id)
    if result is None:
        # 事件流不存在或已过期：交给下一轮回放 run_events，避免空转。
        await asyncio.sleep(2)
        return [], 2.0
    events, _ = result
    if not events:
        return [], 15.0
    frames = [
        f"id: {event['id']}\nevent: {event['kind']}\ndata: {json.dumps(event['payload'], ensure_ascii=False, default=str)}\n\n"
        for event in events
    ]
    return frames, 0.0
