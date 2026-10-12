"""SQLAlchemy 元数据基类与归属混入；核心表和各模块的表共用同一份元数据。"""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import uuid4

from sqlalchemy import DateTime, String
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


def now() -> datetime:
    """返回带时区的 UTC 时间，数据库和 Worker 的租约使用同一时间语义。"""
    return datetime.now(UTC)


def identifier() -> str:
    """生成不包含身份或路径信息的资源 ID。"""
    return str(uuid4())


class Base(DeclarativeBase):
    """统一 SQLAlchemy 元数据，迁移以此定义为基础。"""


class Owned:
    """所有业务资源均携带租户及主体；事务 RLS 和应用查询双重限制访问。"""

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=identifier)
    tenant_id: Mapped[str] = mapped_column(String(255), index=True)
    subject_id: Mapped[str] = mapped_column(String(255), index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
