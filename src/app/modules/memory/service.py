"""长期记忆服务：基于 LangGraph AsyncPostgresStore 的语义检索与持久化。"""

from __future__ import annotations

from contextlib import AbstractAsyncContextManager
from datetime import timedelta
from typing import TYPE_CHECKING, cast

from sqlalchemy import delete, select, text

from app.core.errors import AppError, Code
from app.core.telemetry import retrieval
from app.db.models import now
from app.db.repository import attributes, owned, serialize
from app.modules.memory.models import Memory

if TYPE_CHECKING:
    from langgraph.store.postgres import AsyncPostgresStore


class MemoryService:
    """长期记忆的增删查与语义检索；使用 LangGraph store 实现持久化。"""

    def __init__(self, resources):
        self.resources = resources
        self.db = resources.db
        self.store: AsyncPostgresStore | None = None
        self._embedding_model = None

    async def initialize(self):
        """初始化 LangGraph AsyncPostgresStore。"""
        from langgraph.store.postgres import AsyncPostgresStore

        # Store 使用 Psycopg 自己的异步池；由容器退出栈统一关闭。
        conn_string = self.resources.settings.database_url.get_secret_value()
        store_context = cast(
            AbstractAsyncContextManager[AsyncPostgresStore],
            AsyncPostgresStore.from_conn_string(conn_string),
        )
        self.store = await self.resources.stack.enter_async_context(store_context)
        await self.store.setup()

    async def put(
        self, principal, namespace: str, key: str, content: str, sources=None, metadata=None
    ):
        """按命名空间及键原子更新，保留来源与递增版本；支持语义检索。"""
        from sqlalchemy.dialects.postgresql import insert

        retention = self.resources.settings.memory_retention_days

        # 生成 embedding
        embedding = None
        if self._embedding_model:
            embeddings = await self.resources.models.embed(self._embedding_model, [content])
            embedding = embeddings[0] if embeddings else None

        values = dict(
            **attributes(principal),
            namespace=namespace,
            key=key,
            content=content,
            sources=sources or [],
            metadata_=metadata or {},
            updated_at=now(),
            expires_at=now() + timedelta(days=retention) if retention else None,
            embedding=embedding,
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
                    "embedding": embedding,
                    "version": Memory.version + 1,
                },
            )
            row = (await session.scalars(statement.returning(Memory))).one()

            # 同步到 LangGraph store
            if self.store:
                store_namespace = (str(principal.tenant_id), str(principal.subject_id), namespace)
                await self.store.aput(
                    store_namespace,
                    key,
                    {
                        "content": content,
                        "sources": sources or [],
                        "metadata": metadata or {},
                        "version": row.version,
                    },
                )

            return serialize(row)

    async def get(self, principal, namespace: str, key: str):
        """获取单个记忆。"""
        async with self.db.transaction(principal) as session:
            row = (
                await session.scalars(
                    select(Memory).where(
                        Memory.namespace == namespace,
                        Memory.key == key,
                        (Memory.expires_at.is_(None) | (Memory.expires_at > now())),
                        *owned(Memory, principal),
                    )
                )
            ).one_or_none()
            if row is None:
                raise AppError(Code.RESOURCE_NOT_FOUND, "记忆不存在", 404)
            return serialize(row)

    async def search(self, principal, query: str, namespace: str | None, limit=20):
        """以显式全文匹配检索长期记忆，过滤过期条目。"""
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

    async def semantic_search(
        self, principal, query: str, namespace: str | None, top_k=10, min_score=0.7
    ):
        """语义检索：使用 embedding 向量相似度查找相关记忆。"""
        if not self._embedding_model:
            raise AppError(Code.EMBEDDING_CONFIGURATION, "语义检索需要配置 embedding 模型", 503)

        with retrieval.time():
            # 生成查询向量
            query_embeddings = await self.resources.models.embed(self._embedding_model, [query])
            query_vector = query_embeddings[0]

            from pgvector.sqlalchemy import HALFVEC, Vector
            from sqlalchemy import cast, func

            dimensions = len(query_vector)
            expression = (
                cast(Memory.embedding, Vector(dimensions))
                if dimensions <= 2000
                else cast(Memory.embedding, HALFVEC(dimensions))
            )
            distance = expression.cosine_distance(query_vector).label("distance")

            async with self.db.transaction(principal) as session:
                await session.execute(text("SET LOCAL hnsw.iterative_scan = strict_order"))

                filters = [
                    Memory.embedding.isnot(None),
                    func.vector_dims(Memory.embedding) == dimensions,
                    (Memory.expires_at.is_(None) | (Memory.expires_at > now())),
                    *owned(Memory, principal),
                ]
                if namespace:
                    filters.append(Memory.namespace == namespace)

                stmt = select(Memory, distance).where(*filters).order_by(distance).limit(top_k)

                rows = await session.execute(stmt)

                results = []
                for memory, dist in rows.all():
                    score = 1.0 - float(dist)  # 转为相似度分数
                    if score >= min_score:
                        result = serialize(memory)
                        result["score"] = score
                        results.append(result)

                return results

    def enable_semantic_search(self, embedding_model: str):
        """启用语义检索功能。"""
        self._embedding_model = embedding_model

    async def list_namespaces(self, principal, limit=100):
        """列出当前用户的所有命名空间。"""
        async with self.db.transaction(principal) as session:
            stmt = (
                select(Memory.namespace)
                .where(
                    *owned(Memory, principal),
                    (Memory.expires_at.is_(None) | (Memory.expires_at > now())),
                )
                .distinct()
                .limit(limit)
            )
            rows = await session.scalars(stmt)
            return [{"namespace": ns} for ns in rows.all()]

    async def delete(self, principal, resource_id: str):
        """永久删除当前主体的一条记忆；不存在时准确返回 404。"""
        async with self.db.transaction(principal) as session:
            # 获取记忆信息用于同步删除 store
            memory = (
                await session.scalars(
                    select(Memory).where(Memory.id == resource_id, *owned(Memory, principal))
                )
            ).one_or_none()

            if memory is None:
                raise AppError(Code.RESOURCE_NOT_FOUND, "记忆不存在", 404)

            # 从 LangGraph store 删除
            if self.store:
                store_namespace = (
                    str(memory.tenant_id),
                    str(memory.subject_id),
                    memory.namespace,
                )
                await self.store.adelete(store_namespace, memory.key)

            # 从数据库删除
            result = await session.execute(
                delete(Memory)
                .where(Memory.id == resource_id, *owned(Memory, principal))
                .returning(Memory.id)
            )
            if result.scalar_one_or_none() is None:
                raise AppError(Code.RESOURCE_NOT_FOUND, "记忆不存在", 404)

    async def cleanup(self) -> None:
        """删除已过期的记忆；未设置过期时间的记忆不会被清理。"""
        async with self.db.transaction(system=True) as session:
            await session.execute(delete(Memory).where(Memory.expires_at < now()))
