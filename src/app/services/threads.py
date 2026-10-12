"""线程服务：创建、删除线程及读取原生状态；不维护与框架检查点竞争的第二份会话历史。"""

from __future__ import annotations

import hashlib

from sqlalchemy import select

from app.core.errors import AppError
from app.db.models import Thread
from app.db.repository import attributes, owned, serialize


class ThreadService:
    """线程的创建、删除与状态读取；删除前拒绝仍有活动运行的线程。"""

    def __init__(self, resources):
        self.resources = resources
        self.db = resources.db

    async def create(self, principal, title: str | None = None):
        """建立仅当前主体可访问的线程，不产生业务消息或示例状态。"""
        async with self.db.transaction(principal) as session:
            row = Thread(**attributes(principal), title=title)
            session.add(row)
            await session.flush()
            return serialize(row)

    async def delete(self, principal, thread_id: str):
        """删除前拒绝仍有活动运行的线程；原生检查点先删除，成功后删除归属记录。"""
        async with self.db.transaction(principal) as session:
            row = (
                await session.scalars(
                    select(Thread)
                    .where(Thread.id == thread_id, *owned(Thread, principal))
                    .with_for_update()
                )
            ).one_or_none()
            if row is None:
                raise AppError("resource_not_found", "线程不存在", 404)
            if row.active_run:
                raise AppError("thread_busy", "线程有未结束运行", 409)
            # 先保持行锁阻止新运行，再删除原生状态；删除失败不移除归属记录。
            namespace = hashlib.sha256(
                f"{principal.tenant_id}\0{principal.subject_id}\0{thread_id}".encode()
            ).hexdigest()
            saver = await self.resources.checkpoint()
            await saver.adelete_thread(namespace)
            await session.delete(row)

    async def state(self, principal, thread_id: str):
        """授权后查询原生状态；不维护第二份会话历史，也不开放检查点命名空间参数。"""
        from app.db.models import Run

        await self.resources.repository.get(Thread, thread_id, principal)
        async with self.db.transaction(principal) as session:
            run = (
                await session.scalars(
                    select(Run)
                    .where(Run.thread_id == thread_id, *owned(Run, principal))
                    .order_by(Run.created_at.desc())
                    .limit(1)
                )
            ).first()
            if run is None:
                return {"framework": None, "state": None}
            registration = self.resources.agents.get(run.agent_id)
            if registration.framework == "llamaindex":
                return {
                    "framework": "llamaindex",
                    "context": run.snapshot.get("context") if run.snapshot else None,
                }
        namespace = hashlib.sha256(
            f"{principal.tenant_id}\0{principal.subject_id}\0{thread_id}".encode()
        ).hexdigest()
        saver = await self.resources.checkpoint()
        checkpoint = await saver.aget_tuple({"configurable": {"thread_id": namespace}})
        if checkpoint is None:
            return {"framework": registration.framework, "state": None}

        def convert(value):
            """原生消息使用 JSON 模型序列化，未知类型给出准确序列化错误。"""
            if hasattr(value, "model_dump"):
                return value.model_dump(mode="json")
            raise TypeError("原生状态不能序列化为 JSON")

        import json

        return {
            "framework": registration.framework,
            "state": json.loads(
                json.dumps(checkpoint.checkpoint["channel_values"], default=convert)
            ),
        }
