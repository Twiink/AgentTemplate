"""资源与长期记忆服务；所有查询同时限制租户和主体，HTTP 只负责协议转换。"""

from __future__ import annotations

import hashlib
from datetime import timedelta
from typing import Any

from sqlalchemy import delete, select

from app.core.errors import AppError
from app.db.models import Artifact, Memory, Thread, identifier, now


def owned(model, principal):
    """应用级归属谓词；即使误用特权数据库账号，也不依赖 RLS 单独隔离。"""
    return [model.tenant_id == principal.tenant_id, model.subject_id == principal.subject_id]


def attributes(principal):
    """由可信主体生成写入字段，调用者不能从请求体覆盖身份。"""
    return {"tenant_id": principal.tenant_id, "subject_id": principal.subject_id}


def serialize(row) -> dict[str, Any]:
    """只序列化自有资源公开字段，隐藏连接、内部租约、对象键及恢复状态。"""
    hidden = {
        "tenant_id",
        "subject_id",
        "scopes",
        "lease_token",
        "lease_until",
        "trace_context",
        "snapshot",
        "resume",
        "object_key",
        "options",
    }
    result = {}
    for column in row.__table__.columns:
        if column.key in hidden:
            continue
        value = getattr(row, "metadata_" if column.key == "metadata" else column.key)
        if hasattr(value, "isoformat"):
            value = value.isoformat()
        result[column.key] = value
    return result


class DataService:
    """线程、记忆和附件的真实 CRUD；数据库失败时回滚，存储失败时不伪装成功。"""

    def __init__(self, resources):
        self.resources = resources
        self.db = resources.db

    async def create_thread(self, principal, title: str | None = None):
        """建立仅当前主体可访问的线程，不产生业务消息或示例状态。"""
        async with self.db.transaction(principal) as session:
            row = Thread(**attributes(principal), title=title)
            session.add(row)
            await session.flush()
            return serialize(row)

    async def list(self, model, principal, limit=50, offset=0, filters=()):
        """统一分页查询，结果总是限制当前资源归属。"""
        async with self.db.transaction(principal) as session:
            rows = (
                await session.scalars(
                    select(model)
                    .where(*owned(model, principal), *filters)
                    .order_by(model.created_at.desc(), model.id)
                    .limit(limit)
                    .offset(offset)
                )
            ).all()
            return [serialize(row) for row in rows]

    async def get(self, model, resource_id: str, principal):
        """读取资源；跨主体与不存在均返回 404，避免泄露资源是否存在。"""
        async with self.db.transaction(principal) as session:
            row = (
                await session.scalars(
                    select(model).where(model.id == resource_id, *owned(model, principal))
                )
            ).one_or_none()
            if row is None:
                raise AppError("resource_not_found", "资源不存在", 404)
            return serialize(row)

    async def delete_thread(self, principal, thread_id: str):
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

    async def put_memory(
        self, principal, namespace: str, key: str, content: str, sources=None, metadata=None
    ):
        """按命名空间及键原子更新，保留来源与递增版本；配置保留期时写入过期时间。"""
        from sqlalchemy.dialects.postgresql import insert

        retention = self.resources.settings.memory_retention_days
        values = dict(
            **attributes(principal),
            namespace=namespace,
            key=key,
            content=content,
            sources=sources or [],
            metadata_=metadata or {},
            updated_at=now(),
            expires_at=now() + timedelta(days=retention) if retention else None,
        )
        async with self.db.transaction(principal) as session:
            statement = insert(Memory).values(**values)
            statement = statement.on_conflict_do_update(
                index_elements=[Memory.tenant_id, Memory.subject_id, Memory.namespace, Memory.key],
                set_={
                    "content": content,
                    "sources": sources or [],
                    "metadata": metadata or {},
                    "updated_at": now(),
                    "expires_at": values["expires_at"],
                    "version": Memory.version + 1,
                },
            )
            row = (await session.scalars(statement.returning(Memory))).one()
            return serialize(row)

    async def search_memory(self, principal, query: str, namespace: str | None, limit=20):
        """以显式全文匹配检索长期记忆，过滤过期条目，不假装是向量语义查询。"""
        slash = chr(92)
        escaped = (
            query.replace(slash, slash * 2).replace("%", slash + "%").replace("_", slash + "_")
        )
        filters = [
            Memory.content.ilike(f"%{escaped}%", escape=slash),
            (Memory.expires_at.is_(None) | (Memory.expires_at > now())),
        ]
        if namespace:
            filters.append(Memory.namespace == namespace)
        return await self.list(Memory, principal, limit=limit, filters=filters)

    async def delete_memory(self, principal, resource_id: str):
        """永久删除当前主体的一条记忆；不存在时准确返回 404。"""
        async with self.db.transaction(principal) as session:
            result = await session.execute(
                delete(Memory)
                .where(Memory.id == resource_id, *owned(Memory, principal))
                .returning(Memory.id)
            )
            if result.scalar_one_or_none() is None:
                raise AppError("resource_not_found", "记忆不存在", 404)

    async def upload(self, principal, filename: str, content_type: str, data: bytes):
        """先保存实际内容再登记元数据；登记失败立即清理孤立对象。"""
        if len(data) > self.resources.settings.storage.max_bytes:
            raise AppError("artifact_too_large", "附件超过大小限制", 413)
        key = identifier()
        await self.resources.storage.put(key, data, content_type)
        try:
            async with self.db.transaction(principal) as session:
                row = Artifact(
                    **attributes(principal),
                    filename=filename[:255],
                    content_type=content_type[:150],
                    object_key=key,
                    size=len(data),
                    sha256=hashlib.sha256(data).hexdigest(),
                )
                session.add(row)
                await session.flush()
                return serialize(row)
        except BaseException:
            await self.resources.storage.delete(key)
            raise

    async def download(self, principal, artifact_id: str):
        """授权后才读取存储；返回元信息和内容供 API 或导入任务使用。"""
        async with self.db.transaction(principal) as session:
            row = (
                await session.scalars(
                    select(Artifact).where(Artifact.id == artifact_id, *owned(Artifact, principal))
                )
            ).one_or_none()
            if row is None:
                raise AppError("resource_not_found", "附件不存在", 404)
            metadata, key = serialize(row), row.object_key
        return metadata, await self.resources.storage.get(key)

    async def delete_artifact(self, principal, artifact_id: str):
        """附件被导入记录引用时拒绝删除；独立对象先删除实际内容再删除元数据。"""
        from app.db.models import ImportJob

        async with self.db.transaction(principal) as session:
            row = (
                await session.scalars(
                    select(Artifact)
                    .where(Artifact.id == artifact_id, *owned(Artifact, principal))
                    .with_for_update()
                )
            ).one_or_none()
            if row is None:
                raise AppError("resource_not_found", "附件不存在", 404)
            reference = (
                await session.scalars(select(ImportJob).where(ImportJob.artifact_id == artifact_id))
            ).first()
            if reference:
                raise AppError("artifact_in_use", "附件仍被知识导入记录引用", 409)
            await self.resources.storage.delete(row.object_key)
            await session.delete(row)

    async def thread_state(self, principal, thread_id: str):
        """授权后查询原生状态；不维护第二份会话历史，也不开放检查点命名空间参数。"""
        from app.db.models import Run

        await self.get(Thread, thread_id, principal)
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
