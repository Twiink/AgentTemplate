"""后台运行：提交时在同一事务写入运行与 outbox，调度进程投递到 Celery；失效租约重新排队，定期清理。"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from datetime import timedelta
from typing import Any

from opentelemetry import propagate
from pydantic import ValidationError
from sqlalchemy import delete, select

from app.core.errors import AppError
from app.db.models import Run, Thread, ToolCall, now
from app.db.repository import attributes, owned, serialize
from app.modules.background.models import Outbox

Hook = Callable[[], Awaitable[Any]]


class BackgroundService:
    """后台运行的提交、人工响应后的重新排队、outbox 投递、租约恢复与保留期清理。"""

    def __init__(self, resources):
        self.resources = resources
        self.db = resources.db
        # 其他模块注册的租约回收与清理任务，由 recover、cleanup 统一调度。
        self.recovery_hooks: list[Hook] = []
        self.cleanup_hooks: list[Hook] = []

    async def submit(self, principal, agent_id, thread_id, input, options=None):
        """校验注册 Schema 后锁住线程并创建排队运行和投递记录，返回 202 使用的资源。"""
        item = self.resources.agents.get(agent_id)
        # 未声明 input_schema 的 Agent 直接接收 messages 形态的输入，不做额外校验。
        if item.spec.input_schema is not None:
            try:
                input = item.spec.input_schema.model_validate(input).model_dump(mode="json")
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
            await self.resources.runs.append_event(session, run, "status", {"status": "queued"})
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
            await self.resources.runs.append_event(session, run, "status", {"status": "queued"})
            return serialize(run)

    async def dispatch(self):
        """发布待投递记录并确认；发布后崩溃会再次投递，run claim 处理重复。

        排队与执行中的运行数量由 API 进程的后台任务统一采集，这里不再重复设置指标。
        """
        from app.modules.background.celery_app import celery_app

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
                # outbox 的 task 与 Celery 任务名一一对应：run、knowledge、memory。
                await asyncio.to_thread(
                    celery_app.send_task, "agent." + row.task, args=[row.resource_id]
                )
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
                await self.resources.runs.append_event(
                    session, run, "status", {"status": "queued", "reason": "lease_expired"}
                )
        # 其他模块的后台任务（如知识导入）各自回收失效租约。
        for hook in self.recovery_hooks:
            await hook()
        return len(rows)

    async def cleanup(self):
        """清理保留期外的终态运行、各模块登记的过期数据，以及 7 天前已投递的 outbox 记录。"""
        await self.resources.runs.cleanup()
        for hook in self.cleanup_hooks:
            await hook()
        async with self.db.transaction(system=True) as session:
            await session.execute(
                delete(Outbox).where(Outbox.delivered_at < now() - timedelta(days=7))
            )
