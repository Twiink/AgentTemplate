"""运行服务：运行租约、线程互斥、事件序号与执行；后台提交与投递由 background 模块负责。"""

from __future__ import annotations

import asyncio
import json
import time
from collections.abc import Awaitable, Callable
from datetime import timedelta
from typing import Any
from uuid import uuid4

import structlog
from opentelemetry import propagate
from sqlalchemy import delete, select

from app.core.errors import AppError, Code
from app.core.security import Principal
from app.core.telemetry import run_duration_seconds
from app.core.telemetry import runs_total as run_metric
from app.db.models import Run, RunEvent, Thread, now
from app.db.repository import attributes, owned, serialize
from app.runtime.contracts import RunContext, current_run
from app.runtime.events import TERMINAL, Event


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
        # 运行进入任一终态时执行，例如记录失败或取消任务已经产生的模型费用。
        self.terminal_hooks: list[Callable[[Any, Run], Awaitable[Any]]] = []

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
        """追加事件时核验租约所有者，过期 Worker 不能覆盖或继续发布结果。

        落库与推送分开：关键事件写 run_events，全部事件同时进入进程内总线，
        使请求内的流式订阅能立即收到增量，而不必轮询数据库。
        """
        from app.runtime.events import canonical

        payload = json.loads(json.dumps(payload, default=str))
        name, persist = canonical(kind, payload)
        sequence = None
        if persist:
            async with self.db.transaction(context.principal) as session:
                run = (
                    await session.scalars(
                        select(Run)
                        .where(Run.id == context.run_id, *owned(Run, context.principal))
                        .with_for_update()
                    )
                ).one()
                if run.lease_token != context.lease_token or run.status != "running":
                    raise AppError(Code.LEASE_LOST, "执行租约已失效", 409)
                await self.append_event(session, run, kind, payload)
                sequence = run.event_sequence
        self.resources.buses.publish(
            context.run_id, Event(name=name, payload=payload, sequence=sequence, persisted=persist)
        )
        # 后台档需要跨进程送达，因此关键事件与增量同时进入 Redis Streams；Redis 故障时
        # 订阅端回落到 run_events 回放，这里不会因为发布失败而中断执行。
        await self.resources.streams.append(
            context.run_id, Event(name=name, payload=payload, sequence=sequence, persisted=persist)
        )

    async def persist_usage(self, context):
        """每次模型调用后保存累计用量，恢复时不重置已消费预算。"""
        async with self.db.transaction(context.principal) as session:
            run = (
                await session.scalars(select(Run).where(Run.id == context.run_id).with_for_update())
            ).one()
            if run.lease_token != context.lease_token:
                raise AppError(Code.LEASE_LOST, "执行租约已失效", 409)
            run.usage = dict(context.usage)

    async def snapshot(self, context, payload):
        """保存 JSON 快照；序列化失败不能记录成恢复成功。"""
        payload = json.loads(json.dumps(payload))
        async with self.db.transaction(context.principal) as session:
            run = (
                await session.scalars(select(Run).where(Run.id == context.run_id).with_for_update())
            ).one()
            if run.lease_token != context.lease_token:
                raise AppError(Code.LEASE_LOST, "执行租约已失效", 409)
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
            # 记录队列等待时间：从创建到领取的耗时
            wait_seconds = (now() - run.created_at).total_seconds()
            from app.core.telemetry import run_queue_wait_seconds

            run_queue_wait_seconds.observe(wait_seconds)

            token = str(uuid4())
            run.status, run.lease_token = "running", token
            run.lease_until = now() + timedelta(seconds=self.resources.settings.lease_seconds)
            await self.append_event(session, run, "status", {"status": "running"})
            sequence = run.event_sequence
        # 领取成功后把事件流游标抬到当前序号：不落库的增量事件接在它之上编号，
        # 才不会出现小于已落库序号的 ID，续传的订阅端也就不会跳过它们。
        await self.resources.streams.seed(run_id, sequence)
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
            if status in TERMINAL:
                for hook in self.terminal_hooks:
                    await hook(session, run)

    async def submit(self, principal, agent_id, thread_id, input, options=None, *, queued=True):
        """锁住线程并创建运行记录；queued 为真时交给后台投递，否则由请求内直接执行。

        两档共用同一段校验与互斥逻辑，避免同步与异步两条路径出现行为差异。
        """
        from app.db.models import Thread

        # 检查费用配额
        await self.resources.quota.check(principal)

        item = self.resources.agents.get(agent_id)
        # 未声明 input_schema 的 Agent 直接接收 messages 形态的输入，不做额外校验。
        if item.spec.input_schema is not None:
            try:
                input = item.spec.input_schema.model_validate(input).model_dump(mode="json")
            except Exception as error:
                raise AppError(Code.AGENT_INPUT_INVALID, "输入不符合 Agent Schema", 422) from error
        options = options or {}
        # 原生配置不允许触及服务端身份字段，否则会绕过线程与检查点隔离。
        native = options.get("langgraph", {})
        if "configurable" in native or "thread_id" in options:
            raise AppError(Code.RESERVED_CONFIGURATION, "运行配置不能覆盖服务端线程身份")
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
                raise AppError(Code.RESOURCE_NOT_FOUND, "线程不存在", 404)
            if thread.active_run:
                raise AppError(Code.THREAD_BUSY, "同一线程已有活动运行", 409)
            run = Run(
                **attributes(principal),
                agent_id=agent_id,
                thread_id=thread_id,
                input=input,
                options=options,
                status="queued" if queued else "running",
                scopes=sorted(principal.scopes),
                trace_context=carrier,
            )
            session.add(run)
            await session.flush()
            thread.active_run = run.id
            if queued:
                # 后台档：同一事务写入 outbox，由调度进程投递。
                from app.modules.background.models import Outbox

                session.add(Outbox(**attributes(principal), task="run", resource_id=run.id))
            await self.append_event(session, run, "status", {"status": "queued"})
            return serialize(run)

    async def resume(self, principal, run_id, response, expected_sequence):
        """消费等待状态并继续执行；expected_sequence 防止重复提交与旧页面误操作。"""
        from app.db.models import Run as RunModel

        async with self.db.transaction(principal) as session:
            run = (
                await session.scalars(
                    select(RunModel)
                    .where(RunModel.id == run_id, *owned(RunModel, principal))
                    .with_for_update()
                )
            ).one_or_none()
            if run is None:
                raise AppError(Code.RESOURCE_NOT_FOUND, "运行不存在", 404)
            if run.status != "waiting_input" or run.event_sequence != expected_sequence:
                raise AppError(Code.RESUME_CONFLICT, "运行已恢复或响应版本过期", 409)
            if run.cancel_requested:
                raise AppError(Code.RUN_CANCELLED, "运行已取消", 409)
            run.resume, run.status, run.pending = response, "queued", None
            await self.append_event(session, run, "status", {"status": "queued"})
            return serialize(run)

    async def run_inline(self, principal, run_id: str):
        """在请求内执行运行；客户端断开时由调用方取消任务，检查点保留可继续。"""
        await self.execute(run_id)
        return await self.resources.repository.get(Run, run_id, principal)

    async def finish_cancelled(self, principal, run_id: str) -> None:
        """把已取消的请求内运行置为终态；已在终态时不重复写入。

        异步取消会在 _execute 内完成一次收尾，这里只兜底处理尚未落到终态的情况，
        因此必须幂等，否则事件序号会被重复推进。
        """
        async with self.db.transaction(principal) as session:
            run = (
                await session.scalars(
                    select(Run).where(Run.id == run_id, *owned(Run, principal)).with_for_update()
                )
            ).one_or_none()
            if run is None or run.status in TERMINAL:
                return
            run.status, run.cancel_requested = "cancelled", True
            run.lease_token, run.lease_until = None, None
            run.completed_at = now()
            thread = (
                await session.scalars(
                    select(Thread).where(Thread.id == run.thread_id).with_for_update()
                )
            ).one()
            if thread.active_run == run.id:
                thread.active_run = None
            await self.append_event(session, run, "status", {"status": "cancelled"})

    async def execute(self, run_id: str):
        """持有线程会话级 advisory lock，失效 Worker 尚存时不允许第二实例写检查点。

        锁使用独立的 fence 连接池，不再占用业务连接池；并发运行时锁竞争会等待而不是
        直接失败，避免同一进程内的请求内执行互相拒绝。
        """
        async with self.db.transaction(system=True) as session:
            row = await session.get(Run, run_id)
            if row is None:
                return
            namespace = f"{row.tenant_id}\0{row.subject_id}\0{row.thread_id}"
        key = self.resources.fence.key(namespace)
        fence = await self.resources.fence.hold(key)
        if fence is None:
            return
        try:
            await self._execute(run_id)
        finally:
            await self.resources.fence.release(fence, key)

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
                "estimated_cost": 0.0,
                "reserved_cost": 0.0,
                "missing_usage_calls": 0,
                "cost": None,
                "calls": 0,
                "reserved_tokens": 0,
            },
            reserved_tokens=int((run.usage or {}).get("reserved_tokens", 0)),
        )
        if run.resume and "approvals" in run.resume:
            context.approvals = run.resume["approvals"]
        started = time.monotonic()
        try:
            item = self.resources.agents.get(run.agent_id)
        except AppError as error:
            await self.finish(
                context, "failed", error={"code": error.code, "message": error.message}
            )
            return
        from app.runtime.adapters import LangGraphAdapter, WaitingInput

        adapter = LangGraphAdapter()

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
                            "agent.id": item.spec.id,
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
                            item, context, run.input, run.options, run.snapshot, run.resume
                        )
            finally:
                current_run.reset(token)

        execution = asyncio.create_task(invoke())
        heartbeat = asyncio.create_task(self.heartbeat(context, execution))
        status = "failed"
        try:
            output = await execution
            elapsed = time.monotonic() - started
            context.usage["elapsed_seconds"] = context.usage.get("elapsed_seconds", 0) + elapsed
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
            elapsed = time.monotonic() - started
            context.usage["elapsed_seconds"] = context.usage.get("elapsed_seconds", 0) + elapsed
            status = "waiting_input"
            await self.finish(context, status, pending=waiting.pending)
        except asyncio.CancelledError:
            status = "cancelled"
            await self.finish(context, status)
        except Exception as error:
            elapsed = time.monotonic() - started
            context.usage["elapsed_seconds"] = context.usage.get("elapsed_seconds", 0) + elapsed
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
            # 运行结束前写出剩余增量，并给出事件流的过期时间；Redis 故障只降级。
            run_duration_seconds.labels(run.agent_id, status).observe(time.monotonic() - started)
            await self.resources.streams.close(run.id)
            run_metric.labels(run.agent_id, status).inc()

    async def cancel(self, principal, run_id):
        """取消请求持久化；排队或等待输入直接终止，执行中由心跳协作停止。"""
        async with self.db.transaction(principal) as session:
            run = (
                await session.scalars(
                    select(Run).where(Run.id == run_id, *owned(Run, principal)).with_for_update()
                )
            ).one_or_none()
            if run is None:
                raise AppError(Code.RESOURCE_NOT_FOUND, "运行不存在", 404)
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
