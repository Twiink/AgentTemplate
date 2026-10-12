"""运行服务：运行租约、线程互斥、事件序号与执行；后台提交与投递由 background 模块负责。"""

from __future__ import annotations

import asyncio
import hashlib
import json
import time
from collections.abc import Awaitable, Callable
from datetime import timedelta
from typing import Any
from uuid import uuid4

import structlog
from opentelemetry import propagate
from sqlalchemy import delete, select

from app.core.errors import AppError
from app.core.security import Principal
from app.core.telemetry import runs as run_metric
from app.db.models import Run, RunEvent, Thread, now
from app.db.repository import attributes, owned, serialize
from app.runtime.contracts import RunContext, current_run

TERMINAL = {"succeeded", "failed", "cancelled"}


def classify(error: Exception, context: RunContext) -> dict[str, str]:
    """把运行异常转为可公开的错误码；超时区分整体时间预算耗尽和单次调用超时。"""
    if isinstance(error, AppError):
        return {"code": error.code, "message": error.message}
    if isinstance(error, TimeoutError):
        if time.monotonic() >= context.deadline:
            return {"code": "run_timeout", "message": "运行超过时间预算"}
        return {"code": "upstream_timeout", "message": "模型或工具调用超时"}
    return {"code": "execution_failed", "message": "运行执行失败"}


class RunService:
    """API 和 Worker 共享运行管理；消费者重复投递不会启动两个有效租约。"""

    def __init__(self, resources):
        self.resources, self.db = resources, resources.db
        # 运行成功后在同一事务内执行的回调，例如记忆模块据此安排提取任务。
        self.succeeded_hooks: list[Callable[[Any, Run], Awaitable[Any]]] = []

    async def append_event(self, session, run, kind, payload):
        """调用者必须已持有运行行锁；序号与事件同事务保存。"""
        run.event_sequence += 1
        session.add(
            RunEvent(
                **attributes(Principal(run.tenant_id, run.subject_id, frozenset())),
                run_id=run.id,
                sequence=run.event_sequence,
                kind=kind,
                payload=payload,
            )
        )

    async def emit(self, context, kind, payload):
        """追加事件时核验租约所有者，过期 Worker 不能覆盖或继续发布结果。"""
        payload = json.loads(json.dumps(payload, default=str))
        async with self.db.transaction(context.principal) as session:
            run = (
                await session.scalars(
                    select(Run)
                    .where(Run.id == context.run_id, *owned(Run, context.principal))
                    .with_for_update()
                )
            ).one()
            if run.lease_token != context.lease_token or run.status != "running":
                raise AppError("lease_lost", "执行租约已失效", 409)
            await self.append_event(session, run, kind, payload)

    async def persist_usage(self, context):
        """每次模型调用后保存累计用量，恢复时不重置已消费预算。"""
        async with self.db.transaction(context.principal) as session:
            run = (
                await session.scalars(select(Run).where(Run.id == context.run_id).with_for_update())
            ).one()
            if run.lease_token != context.lease_token:
                raise AppError("lease_lost", "执行租约已失效", 409)
            run.usage = dict(context.usage)

    async def snapshot(self, context, payload):
        """保存 JSON 快照；序列化失败不能记录成恢复成功。"""
        payload = json.loads(json.dumps(payload))
        async with self.db.transaction(context.principal) as session:
            run = (
                await session.scalars(select(Run).where(Run.id == context.run_id).with_for_update())
            ).one()
            if run.lease_token != context.lease_token:
                raise AppError("lease_lost", "执行租约已失效", 409)
            run.snapshot = payload
            run.usage = dict(context.usage)

    async def claim(self, run_id):
        """原子领取排队运行；只允许一个 Worker 获得有效 token。"""
        async with self.db.transaction(system=True) as session:
            run = (
                await session.scalars(select(Run).where(Run.id == run_id).with_for_update())
            ).one_or_none()
            if run is None or run.status != "queued":
                return None
            if run.cancel_requested:
                run.status, run.completed_at = "cancelled", now()
                thread = await session.get(Thread, run.thread_id)
                thread.active_run = None
                await self.append_event(session, run, "status", {"status": "cancelled"})
                return None
            token = str(uuid4())
            run.status, run.lease_token = "running", token
            run.lease_until = now() + timedelta(seconds=self.resources.settings.lease_seconds)
            await self.append_event(session, run, "status", {"status": "running"})
            return run

    async def heartbeat(self, context, execution):
        """续租并检查取消；失去租约或收到取消时终止当前协程，释放资源。"""
        while not execution.done():
            await asyncio.sleep(self.resources.settings.lease_seconds / 3)
            async with self.db.transaction(context.principal) as session:
                run = (
                    await session.scalars(
                        select(Run).where(Run.id == context.run_id).with_for_update()
                    )
                ).one()
                if run.lease_token != context.lease_token or run.cancel_requested:
                    execution.cancel()
                    return
                run.lease_until = now() + timedelta(seconds=self.resources.settings.lease_seconds)
                run.usage = dict(context.usage)

    async def finish(self, context, status, output=None, error=None, pending=None):
        """完成时再核验租约；等待输入仍占用线程，终态释放线程。"""
        async with self.db.transaction(context.principal) as session:
            run = (
                await session.scalars(select(Run).where(Run.id == context.run_id).with_for_update())
            ).one()
            if run.lease_token != context.lease_token:
                return
            if run.cancel_requested:
                status, output, pending = "cancelled", None, None
            run.status, run.output, run.error, run.pending = status, output, error, pending
            run.usage = context.usage
            run.prompt_versions = {**run.prompt_versions, **context.prompt_versions}
            run.lease_token, run.lease_until = None, None
            if status in TERMINAL:
                run.completed_at = now()
                thread = (
                    await session.scalars(
                        select(Thread).where(Thread.id == run.thread_id).with_for_update()
                    )
                ).one()
                if thread.active_run == run.id:
                    thread.active_run = None
            await self.append_event(session, run, "status", {"status": status, "pending": pending})
            if status == "succeeded":
                for hook in self.succeeded_hooks:
                    await hook(session, run)

    async def execute(self, run_id: str):
        """持有线程会话级 advisory lock，失效 Worker 尚存时不允许第二实例写检查点。"""
        from sqlalchemy import text

        async with self.db.transaction(system=True) as session:
            row = await session.get(Run, run_id)
            if row is None:
                return
            namespace = f"{row.tenant_id}\0{row.subject_id}\0{row.thread_id}"
        key = int.from_bytes(hashlib.sha256(namespace.encode()).digest()[:8], "big", signed=True)
        async with self.db.engine.connect() as fence:
            acquired = (
                await fence.execute(text("SELECT pg_try_advisory_lock(:key)"), {"key": key})
            ).scalar_one()
            await fence.commit()
            if not acquired:
                return
            try:
                await self._execute(run_id)
            finally:
                await fence.execute(text("SELECT pg_advisory_unlock(:key)"), {"key": key})
                await fence.commit()

    async def _execute(self, run_id: str):
        """运行原生适配器并维护心跳；错误只保存脱敏代码和描述，收费调用不自动重复。"""
        run = await self.claim(run_id)
        if run is None:
            return
        settings = self.resources.settings
        principal = Principal(run.tenant_id, run.subject_id, frozenset(run.scopes))
        context = RunContext(
            principal,
            run.id,
            run.thread_id,
            run.lease_token,
            self.resources,
            time.monotonic()
            + max(
                0.01,
                settings.run_timeout_seconds
                - (run.usage.get("elapsed_seconds", 0) if run.usage else 0),
            ),
            settings.run_token_budget,
            settings.run_cost_budget,
            usage=run.usage
            or {
                "input_tokens": None,
                "output_tokens": None,
                "estimated_tokens": 0,
                "missing_usage_calls": 0,
                "cost": None,
                "calls": 0,
            },
        )
        if run.resume and "approvals" in run.resume:
            context.approvals = run.resume["approvals"]
        started = time.monotonic()
        try:
            registration = self.resources.agents.get(run.agent_id)
        except AppError as error:
            await self.finish(
                context, "failed", error={"code": error.code, "message": error.message}
            )
            return
        from app.runtime.adapters import LangGraphAdapter, LlamaIndexAdapter, WaitingInput

        adapter = (
            LlamaIndexAdapter() if registration.framework == "llamaindex" else LangGraphAdapter()
        )

        async def invoke():
            """ContextVar 在执行任务自身设置，使工具和模型共享可信身份及预算。"""
            token = current_run.set(context)
            # 运行任务内产生的日志都带上运行与 Agent 标识。
            structlog.contextvars.bind_contextvars(run_id=run.id, agent_id=run.agent_id)
            try:
                from langsmith.run_helpers import tracing_context

                with (
                    tracing_context(enabled=False),
                    self.resources.telemetry.tracer.start_as_current_span(
                        "Agent 运行",
                        context=propagate.extract(run.trace_context),
                        attributes={
                            "agent.framework": registration.framework,
                            "agent.id": registration.id,
                            "agent.run_id": run.id,
                        },
                    ),
                ):
                    from opentelemetry import trace

                    span = trace.get_current_span()
                    if span.get_span_context().is_valid:
                        async with self.db.transaction(principal) as session:
                            stored = await session.get(Run, run.id)
                            stored.trace_context = {
                                **stored.trace_context,
                                "run_trace_id": format(span.get_span_context().trace_id, "032x"),
                                "run_span_id": format(span.get_span_context().span_id, "016x"),
                            }
                    async with asyncio.timeout(max(0.01, context.deadline - time.monotonic())):
                        return await adapter.execute(
                            registration, context, run.input, run.options, run.snapshot, run.resume
                        )
            finally:
                current_run.reset(token)

        execution = asyncio.create_task(invoke())
        heartbeat = asyncio.create_task(self.heartbeat(context, execution))
        status = "failed"
        try:
            output = await execution
            context.usage["elapsed_seconds"] = (
                context.usage.get("elapsed_seconds", 0) + time.monotonic() - started
            )
            if registration.output_schema:
                output = registration.output_schema.model_validate(output).model_dump(mode="json")
            else:
                output = json.loads(
                    json.dumps(
                        output,
                        default=lambda x: (
                            x.model_dump(mode="json") if hasattr(x, "model_dump") else str(x)
                        ),
                    )
                )
            status = "succeeded"
            await self.finish(context, status, output=output)
        except WaitingInput as waiting:
            context.usage["elapsed_seconds"] = (
                context.usage.get("elapsed_seconds", 0) + time.monotonic() - started
            )
            status = "waiting_input"
            await self.finish(context, status, pending=waiting.pending)
        except asyncio.CancelledError:
            status = "cancelled"
            await self.finish(context, status)
        except Exception as error:
            context.usage["elapsed_seconds"] = (
                context.usage.get("elapsed_seconds", 0) + time.monotonic() - started
            )
            self.resources.logger.error(
                "运行失败",
                run_id=run.id,
                agent_id=run.agent_id,
                exception_type=type(error).__name__,
                exc_info=error,
            )
            public = classify(error, context)
            if isinstance(error, AppError) and error.code == "tool_outcome_unknown":
                status = "waiting_input"
                await self.finish(
                    context,
                    status,
                    pending={"type": "tool_reconciliation", "message": error.message},
                )
            else:
                await self.finish(context, "failed", error=public)
        finally:
            heartbeat.cancel()
            await asyncio.gather(heartbeat, return_exceptions=True)
            run_metric.labels(registration.framework, status).inc()

    async def cancel(self, principal, run_id):
        """取消请求持久化；排队或等待输入直接终止，执行中由心跳协作停止。"""
        async with self.db.transaction(principal) as session:
            run = (
                await session.scalars(
                    select(Run).where(Run.id == run_id, *owned(Run, principal)).with_for_update()
                )
            ).one_or_none()
            if run is None:
                raise AppError("resource_not_found", "运行不存在", 404)
            if run.status in TERMINAL:
                return serialize(run)
            run.cancel_requested = True
            if run.status in {"queued", "waiting_input"}:
                run.status, run.completed_at = "cancelled", now()
                thread = await session.get(Thread, run.thread_id)
                thread.active_run = None
                await self.append_event(session, run, "status", {"status": "cancelled"})
            return serialize(run)

    async def events(self, principal, run_id, after=0):
        """读取完整持久事件分页；不存在和无权限都拒绝，Redis 通知不作为事件来源。"""
        await self.resources.repository.get(Run, run_id, principal)
        async with self.db.transaction(principal) as session:
            rows = (
                await session.scalars(
                    select(RunEvent)
                    .where(
                        RunEvent.run_id == run_id,
                        RunEvent.sequence > after,
                        *owned(RunEvent, principal),
                    )
                    .order_by(RunEvent.sequence)
                    .limit(200)
                )
            ).all()
            return [
                {"sequence": row.sequence, "kind": row.kind, "payload": row.payload} for row in rows
            ]

    async def cleanup(self):
        """删除配置保留期之前结束的终态运行，事件随运行级联删除。"""
        async with self.db.transaction(system=True) as session:
            await session.execute(
                delete(Run).where(
                    Run.status.in_(TERMINAL),
                    Run.completed_at
                    < now() - timedelta(days=self.resources.settings.run_retention_days),
                )
            )
