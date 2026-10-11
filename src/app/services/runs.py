"""持久运行服务；事务投递、运行租约、线程互斥和事件序号全部以 PostgreSQL 为准。"""

from __future__ import annotations

import asyncio
import hashlib
import json
import time
from datetime import timedelta
from uuid import uuid4

from opentelemetry import propagate
from pydantic import ValidationError
from sqlalchemy import delete, select

from app.core.errors import AppError
from app.core.observability import runs as run_metric
from app.core.security import Principal
from app.db.models import Memory, Outbox, Run, RunEvent, Thread, ToolCall, now
from app.runtime.contracts import RunContext, current_run
from app.services.data import attributes, owned, serialize

TERMINAL = {"succeeded", "failed", "cancelled"}


class RunService:
    """API 和 Worker 共享运行管理；消费者重复投递不会启动两个有效租约。"""

    def __init__(self, resources):
        self.resources, self.db = resources, resources.db

    async def submit(self, principal, agent_id, thread_id, input, options=None):
        """校验注册 Schema 后锁住线程并创建排队运行和投递记录，返回 202 使用的资源。"""
        registration = self.resources.agents.get(agent_id)
        try:
            input = registration.input_schema.model_validate(input).model_dump(mode="json")
        except ValidationError as error:
            raise AppError("agent_input_invalid", "输入不符合 Agent Schema", 422) from error
        options = options or {}
        if "configurable" in options or "thread_id" in options:
            raise AppError("reserved_configuration", "运行配置不能覆盖服务端线程身份")
        carrier = {}
        propagate.inject(carrier)
        async with self.db.transaction(principal) as session:
            thread = (
                await session.scalars(
                    select(Thread)
                    .where(Thread.id == thread_id, *owned(Thread, principal))
                    .with_for_update()
                )
            ).one_or_none()
            if thread is None:
                raise AppError("resource_not_found", "线程不存在", 404)
            if thread.active_run:
                raise AppError("thread_busy", "同一线程已有活动运行", 409)
            run = Run(
                **attributes(principal),
                agent_id=agent_id,
                thread_id=thread_id,
                input=input,
                options=options,
                scopes=sorted(principal.scopes),
                trace_context=carrier,
            )
            session.add(run)
            await session.flush()
            thread.active_run = run.id
            session.add(Outbox(**attributes(principal), task="run", resource_id=run.id))
            await self._event(session, run, "status", {"status": "queued"})
            return serialize(run)

    async def _event(self, session, run, kind, payload):
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
            await self._event(session, run, kind, payload)
        await self.resources.redis.publish("run:" + context.run_id, kind)

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
                await self._event(session, run, "status", {"status": "cancelled"})
                return None
            token = str(uuid4())
            run.status, run.lease_token = "running", token
            run.lease_until = now() + timedelta(seconds=self.resources.settings.lease_seconds)
            await self._event(session, run, "status", {"status": "running"})
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
            await self._event(session, run, "status", {"status": status, "pending": pending})
            if status == "succeeded":
                await self.resources.extraction.schedule(session, run)

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
                "运行失败", run_id=run.id, exception_type=type(error).__name__
            )
            public = {
                "code": error.code if isinstance(error, AppError) else "execution_failed",
                "message": error.message if isinstance(error, AppError) else "运行执行失败",
            }
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
                await self._event(session, run, "status", {"status": "cancelled"})
            return serialize(run)

    async def respond(self, principal, run_id, response, expected_sequence):
        """只消费当前等待状态；事件序号避免重复响应或旧页面恢复新一轮等待。"""
        async with self.db.transaction(principal) as session:
            run = (
                await session.scalars(
                    select(Run).where(Run.id == run_id, *owned(Run, principal)).with_for_update()
                )
            ).one_or_none()
            if run is None:
                raise AppError("resource_not_found", "运行不存在", 404)
            if run.status != "waiting_input" or run.event_sequence != expected_sequence:
                raise AppError("resume_conflict", "运行已恢复或响应版本过期", 409)
            if run.cancel_requested:
                raise AppError("run_cancelled", "运行已取消", 409)
            if run.pending and run.pending.get("type") == "tool_reconciliation":
                reconciliation = response.get("reconcile", {})
                call = (
                    await session.scalars(
                        select(ToolCall)
                        .where(
                            ToolCall.run_id == run.id,
                            ToolCall.call_id == reconciliation.get("call_id"),
                            ToolCall.status.in_(["unknown", "executing"]),
                        )
                        .with_for_update()
                    )
                ).one_or_none()
                if call is None:
                    raise AppError(
                        "reconciliation_required", "需要指定未知副作用调用并提供核对结果", 422
                    )
                if "result" in reconciliation:
                    call.status, call.result = "succeeded", reconciliation["result"]
                elif reconciliation.get("confirmed_not_executed") is True:
                    await session.delete(call)
                else:
                    raise AppError("reconciliation_required", "必须提供真实结果或确认未执行", 422)
            run.resume, run.status, run.pending = response, "queued", None
            session.add(Outbox(**attributes(principal), task="run", resource_id=run.id))
            await self._event(session, run, "status", {"status": "queued"})
            return serialize(run)

    async def events(self, principal, run_id, after=0):
        """读取完整持久事件分页；不存在和无权限都拒绝，Redis 通知不作为事件来源。"""
        await self.resources.data.get(Run, run_id, principal)
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

    async def dispatch(self):
        """发布待投递记录并确认；发布后崩溃会再次投递，run claim 处理重复。"""
        from sqlalchemy import func

        from app.core.observability import active_runs, queue_depth
        from app.worker.celery import celery_app

        async with self.db.transaction(system=True) as session:
            queue_depth.set(
                (
                    await session.scalar(
                        select(func.count()).select_from(Run).where(Run.status == "queued")
                    )
                )
                or 0
            )
            active_runs.set(
                (
                    await session.scalar(
                        select(func.count()).select_from(Run).where(Run.status == "running")
                    )
                )
                or 0
            )

        async with self.db.transaction(system=True) as session:
            rows = (
                await session.scalars(
                    select(Outbox)
                    .where(Outbox.delivered_at.is_(None))
                    .order_by(Outbox.created_at)
                    .limit(100)
                    .with_for_update(skip_locked=True)
                )
            ).all()
            for row in rows:
                task = {
                    "run": "agent.run",
                    "knowledge": "agent.knowledge",
                    "memory": "agent.memory",
                }[row.task]
                await asyncio.to_thread(celery_app.send_task, task, args=[row.resource_id])
                row.delivered_at = now()
        return len(rows)

    async def recover(self):
        """回收失效租约并重新投递；正在执行步骤采用至少一次语义，副作用由账本防护。"""
        async with self.db.transaction(system=True) as session:
            rows = (
                await session.scalars(
                    select(Run)
                    .where(Run.status == "running", Run.lease_until < now())
                    .with_for_update(skip_locked=True)
                )
            ).all()
            for run in rows:
                run.status, run.lease_token, run.lease_until = "queued", None, None
                session.add(
                    Outbox(
                        tenant_id=run.tenant_id,
                        subject_id=run.subject_id,
                        task="run",
                        resource_id=run.id,
                    )
                )
                await self._event(
                    session, run, "status", {"status": "queued", "reason": "lease_expired"}
                )
        await self.resources.knowledge.recover()
        return len(rows)

    async def cleanup(self):
        """只清理配置保留期后的终态运行和过期记忆；知识与未过期记忆不自动删除。"""
        async with self.db.transaction(system=True) as session:
            await session.execute(
                delete(Run).where(
                    Run.status.in_(TERMINAL),
                    Run.completed_at
                    < now() - timedelta(days=self.resources.settings.run_retention_days),
                )
            )
            await session.execute(delete(Memory).where(Memory.expires_at < now()))
            await session.execute(
                delete(Outbox).where(Outbox.delivered_at < now() - timedelta(days=7))
            )
