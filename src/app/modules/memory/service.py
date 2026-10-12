"""长期记忆服务：按命名空间与键显式写入、关键词检索、删除，以及过期清理。"""

from __future__ import annotations

from datetime import timedelta

from sqlalchemy import delete

from app.core.errors import AppError
from app.db.models import now
from app.db.repository import attributes, owned, serialize
from app.modules.memory.models import Memory


class MemoryService:
    """显式长期记忆的增删查；所有查询限制当前主体，过期记忆由清理任务删除。"""

    def __init__(self, resources):
        self.resources = resources
        self.db = resources.db

    async def put(
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

    async def search(self, principal, query: str, namespace: str | None, limit=20):
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
        return await self.resources.repository.list(Memory, principal, limit=limit, filters=filters)

    async def delete(self, principal, resource_id: str):
        """永久删除当前主体的一条记忆；不存在时准确返回 404。"""
        async with self.db.transaction(principal) as session:
            result = await session.execute(
                delete(Memory)
                .where(Memory.id == resource_id, *owned(Memory, principal))
                .returning(Memory.id)
            )
            if result.scalar_one_or_none() is None:
                raise AppError("resource_not_found", "记忆不存在", 404)

    async def cleanup(self) -> None:
        """删除已过期的记忆；未设置过期时间的记忆不会被清理。"""
        async with self.db.transaction(system=True) as session:
            await session.execute(delete(Memory).where(Memory.expires_at < now()))
