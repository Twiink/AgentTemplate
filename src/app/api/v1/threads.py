"""线程接口：线程的增删查、原生状态，以及在请求内执行运行的同步入口。

同步执行的三个入口都挂在线程下：`/runs/stream` 流式返回、`/runs/wait` 等待结果、
`/runs/confirm` 提交人工审批后继续。后台执行由 background 模块提供 `POST /runs`。
"""

from __future__ import annotations

import asyncio
from typing import Any

from fastapi import APIRouter, Header, Request
from fastapi.responses import Response, StreamingResponse

from app.api.deps import Identity, Limit, Offset, Resources
from app.db.models import Run, Thread
from app.runtime.bus import sse
from app.runtime.events import Event
from app.schemas.common import Page
from app.schemas.runs import ConfirmInput, RunInput, RunOut
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


async def _start(
    container, principal, thread_id, body: RunInput, idempotency_key: str | None = None
) -> dict:
    """建立并领取一次运行，返回可执行的状态；两档共用同一段提交逻辑。"""
    # 检查幂等性
    if idempotency_key:
        cached = await container.idempotency.get(principal.tenant_id, idempotency_key)
        if cached:
            return cached

        # 尝试获取幂等锁
        if not await container.idempotency.lock(principal.tenant_id, idempotency_key):
            # 已有相同请求正在处理，等待一段时间后重试获取结果
            await asyncio.sleep(1)
            cached = await container.idempotency.get(principal.tenant_id, idempotency_key)
            if cached:
                return cached

    try:
        run = await container.runs.submit(
            principal, body.agent_id, thread_id, body.input, body.options, queued=False
        )

        # 保存幂等性结果
        if idempotency_key:
            await container.idempotency.set(principal.tenant_id, idempotency_key, run)

        return run
    finally:
        if idempotency_key:
            await container.idempotency.unlock(principal.tenant_id, idempotency_key)


async def _execute(container, principal, run_id: str, execution: asyncio.Task):
    """取消执行任务并等待其收尾；客户端断开时不会留下孤儿任务。"""
    execution.cancel()
    await asyncio.gather(execution, return_exceptions=True)
    await container.runs.finish_cancelled(principal, run_id)


@router.post("/threads/{thread_id}/runs/wait", response_model=RunOut)
async def run_wait(
    thread_id: str,
    body: RunInput,
    principal: Identity,
    container: Resources,
    idempotency_key: str | None = Header(None, alias="Idempotency-Key"),
) -> dict:
    """在请求内执行并返回结果；不依赖 Redis，客户端断开时取消执行。"""
    principal.require("resources:write")
    run = await _start(container, principal, thread_id, body, idempotency_key)
    await container.runs.run_inline(principal, run["id"])
    return await container.repository.get(Run, run["id"], principal)


@router.post("/threads/{thread_id}/runs/stream")
async def run_stream(
    thread_id: str,
    body: RunInput,
    request: Request,
    principal: Identity,
    container: Resources,
    idempotency_key: str | None = Header(None, alias="Idempotency-Key"),
) -> StreamingResponse:
    """在请求内执行并以 SSE 返回事件；断开连接时取消执行，检查点保留可继续。"""
    principal.require("resources:write")
    run = await _start(container, principal, thread_id, body, idempotency_key)
    run_id = run["id"]
    bus = container.buses.open(run_id)
    execution = asyncio.create_task(container.runs.run_inline(principal, run_id))

    async def stream():
        """先推送已发生的事件，再持续推送新事件；运行结束时关闭订阅。"""

        queue = bus.subscribe()
        try:
            while True:
                if await request.is_disconnected():
                    await _execute(container, principal, run_id, execution)
                    return
                try:
                    event = await asyncio.wait_for(queue.get(), timeout=15)
                except TimeoutError:
                    # 心跳保持连接；不计入业务事件序号。
                    yield ": 心跳\n\n"
                    continue
                if event is None:
                    state = await container.repository.get(Run, run_id, principal)
                    yield sse(
                        Event(
                            name="run.completed",
                            payload={
                                "status": state["status"],
                                "output": state["output"],
                                "error": state["error"],
                                "usage": state["usage"],
                            },
                        )
                    )
                    return
                yield sse(event)
        finally:
            bus.unsubscribe(queue)
            container.buses.close(run_id)

    return StreamingResponse(
        stream(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@router.post("/threads/{thread_id}/runs/confirm", response_model=RunOut)
async def run_confirm(
    thread_id: str, body: ConfirmInput, principal: Identity, container: Resources
) -> dict:
    """提交人工审批后继续执行；序号不符时拒绝，避免重复恢复。"""
    principal.require("resources:write")
    await container.runs.resume(principal, body.run_id, body.resume, body.expected_sequence)
    await container.runs.run_inline(principal, body.run_id)
    return await container.repository.get(Run, body.run_id, principal)
