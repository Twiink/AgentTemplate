"""附件服务：先保存内容再登记元数据，读取前校验归属，被其他模块引用时拒绝删除。"""

from __future__ import annotations

import hashlib
from collections.abc import Awaitable, Callable

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import AppError
from app.db.models import identifier
from app.db.repository import attributes, owned, serialize
from app.modules.storage.models import Artifact

# 其他模块注册的引用检查：返回 True 表示附件仍被引用，不能删除。
InUseCheck = Callable[[AsyncSession, str], Awaitable[bool]]


class ArtifactService:
    """附件的上传、下载与删除；对象内容由 store 保存，数据库只保存元数据。"""

    def __init__(self, resources, store):
        self.resources = resources
        self.db = resources.db
        self.store = store
        self.guards: list[InUseCheck] = []

    async def upload(self, principal, filename: str, content_type: str, data: bytes):
        """先保存实际内容再登记元数据；登记失败立即清理孤立对象。"""
        if len(data) > self.resources.settings.storage.max_bytes:
            raise AppError("artifact_too_large", "附件超过大小限制", 413)
        key = identifier()
        await self.store.put(key, data, content_type)
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
            await self.store.delete(key)
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
        return metadata, await self.store.get(key)

    async def delete(self, principal, artifact_id: str):
        """附件仍被其他模块引用时拒绝删除；独立对象先删除实际内容再删除元数据。"""
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
            for in_use in self.guards:
                if await in_use(session, artifact_id):
                    raise AppError("artifact_in_use", "附件仍被其他资源引用", 409)
            await self.store.delete(row.object_key)
            await session.delete(row)
