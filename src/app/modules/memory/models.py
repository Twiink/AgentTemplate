"""长期记忆与记忆提取任务表。"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import DateTime, ForeignKey, String, Text, UniqueConstraint
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, Owned, now


class Memory(Owned, Base):
    """显式长期记忆；来源与版本保留，过期策略由清理任务执行。"""

    __tablename__ = "memories"
    namespace: Mapped[str] = mapped_column(String(255), index=True)
    key: Mapped[str] = mapped_column(String(255))
    content: Mapped[str] = mapped_column(Text)
    sources: Mapped[list[dict[str, Any]]] = mapped_column(JSONB, default=list)
    metadata_: Mapped[dict[str, Any]] = mapped_column("metadata", JSONB, default=dict)
    version: Mapped[int] = mapped_column(default=1)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    __table_args__ = (UniqueConstraint("tenant_id", "subject_id", "namespace", "key"),)


class MemoryExtraction(Owned, Base):
    """可选长期记忆提取任务；来源运行唯一关联，重复消息不重复生成记忆。"""

    __tablename__ = "memory_extractions"
    source_run_id: Mapped[str] = mapped_column(
        ForeignKey("runs.id", ondelete="CASCADE"), unique=True
    )
    extraction_run_id: Mapped[str | None] = mapped_column(String(36))
    status: Mapped[str] = mapped_column(String(32), default="queued")
    attempts: Mapped[int] = mapped_column(default=0)
    error: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
