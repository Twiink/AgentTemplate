"""后台运行：提交时在同一事务写入运行与 outbox，调度进程投递到 Celery；失效租约重新排队，定期清理。"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from datetime import timedelta
from typing import Any

from sqlalchemy import delete, select

from app.core.errors import AppError, Code
from app.db.models import Run, ToolCall, now
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
        """校验输入后锁住线程并创建排队运行与投递记录，提交成功后立即投递。

        具体校验与互斥逻辑由运行服务统一实现，两档共用同一段代码。投递必须在事务提交
        之后进行：先发消息再提交会让 Worker 读到尚未可见的运行，是典型的竞态。
        """
        run = await self.resources.runs.submit(
            principal, agent_id, thread_id, input, options, queued=True
        )
        await self.deliver(run["id"])
        return run

    async def deliver(self, run_id: str) -> None:
        """提交后立即投递；投递失败只记录，30 秒周期的 outbox 调度会补发。"""
        try:
            await self.dispatch_run(run_id)
        except Exception as error:
            self.resources.logger.warning(
                "立即投递失败，等待 outbox 补发",
                run_id=run_id,
                exception_type=type(error).__name__,
            )

    async def respond(self, principal, run_id, response, expected_sequence):
        """只消费当前等待状态；事件序号避免重复响应或旧页面恢复新一轮等待。"""
        async with self.db.transaction(principal) as session:
            run = (
                await session.scalars(
                    select(Run).where(Run.id == run_id, *owned(Run, principal)).with_for_update()
                )
            ).one_or_none()
            if run is None:
                raise AppError(Code.RESOURCE_NOT_FOUND, "运行不存在")
            if run.status != "waiting_input" or run.event_sequence != expected_sequence:
                raise AppError(Code.RESUME_CONFLICT, "运行已恢复或响应版本过期")
            if run.cancel_requested:
                raise AppError(Code.RUN_CANCELLED, "运行已取消")
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
                    raise AppError(Code.RECONCILIATION_REQUIRED, "必须提供真实结果或确认未执行")
            run.resume, run.status, run.pending = response, "queued", None
            session.add(Outbox(**attributes(principal), task="run", resource_id=run.id))
            await self.resources.runs.append_event(session, run, "status", {"status": "queued"})
            result = serialize(run)
        # 事务提交后再投递，避免 Worker 读到尚未可见的等待状态。
        await self.deliver(run_id)
        return result

    async def dispatch_run(self, run_id: str) -> int:
        """提交事务后立即投递该运行的待发记录，使排队到运行中的延迟不取决于调度周期。

        与周期调度共用同一领取方式：只处理 `delivered_at` 为空且尚未被其他调度进程
        锁定的记录。并发调用与调度补发都不会重复投递，投递后崩溃的重复由运行 claim 处理。
        """
        from app.modules.background.celery_app import celery_app

        async with self.db.transaction(system=True) as session:
            rows = (
                await session.scalars(
                    select(Outbox)
                    .where(Outbox.delivered_at.is_(None), Outbox.resource_id == run_id)
                    .order_by(Outbox.created_at)
                    .with_for_update(skip_locked=True)
                )
            ).all()
            for row in rows:
                await asyncio.to_thread(
                    celery_app.send_task, "agent." + row.task, args=[row.resource_id]
                )
                row.delivered_at = now()
        return len(rows)

    async def dispatch(self):
        """发布待投递记录并确认；发布后崩溃会再次投递，run claim 处理重复。

        提交路径已经主动投递，这里是 30 秒周期的兜底：投递失败、进程崩溃或 outbox
        写入后未及时提交的记录都由它补发。排队与执行中的运行数量由 API 进程的后台
        任务统一采集，这里不再重复设置指标。
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
