"""带归属条件的通用查询；所有读写同时限制租户与主体，序列化时隐藏内部字段。"""

from __future__ import annotations

from typing import Any

from sqlalchemy import select

from app.core.errors import AppError, Code


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


class Repository:
    """通用分页与单条读取；跨主体与不存在都返回 404，避免泄露资源是否存在。"""

    def __init__(self, db):
        self.db = db

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
                raise AppError(Code.RESOURCE_NOT_FOUND, "资源不存在", 404)
            return serialize(row)
