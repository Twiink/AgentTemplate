"""知识库、不可变向量空间、文档、导入任务与向量片段表。"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from pgvector.sqlalchemy import Vector
from sqlalchemy import DateTime, ForeignKey, Integer, String, Text, UniqueConstraint
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, Owned


class KnowledgeBase(Owned, Base):
    """知识库保存活动向量空间；重建空间发布前旧空间持续提供查询。"""

    __tablename__ = "knowledge_bases"
    name: Mapped[str] = mapped_column(String(255))
    active_index: Mapped[str | None] = mapped_column(String(36))


class KnowledgeIndex(Owned, Base):
    """不可变向量配置版本；不同模型及维度不会进入同一检索空间。"""

    __tablename__ = "knowledge_indexes"
    knowledge_id: Mapped[str] = mapped_column(ForeignKey("knowledge_bases.id", ondelete="CASCADE"))
    profile: Mapped[dict[str, Any]] = mapped_column(JSONB)
    status: Mapped[str] = mapped_column(String(30), default="building")


class Document(Owned, Base):
    """文档逻辑实体；同一知识库按来源名关联新版本。"""

    __tablename__ = "documents"
    knowledge_id: Mapped[str] = mapped_column(ForeignKey("knowledge_bases.id", ondelete="CASCADE"))
    name: Mapped[str] = mapped_column(String(255))
    version: Mapped[int] = mapped_column(default=1)
    __table_args__ = (UniqueConstraint("knowledge_id", "name"),)


class ImportJob(Owned, Base):
    """后台导入记录；状态、取消和错误可查询，未完成时不切换活动索引。"""

    __tablename__ = "import_jobs"
    knowledge_id: Mapped[str] = mapped_column(ForeignKey("knowledge_bases.id", ondelete="CASCADE"))
    index_id: Mapped[str] = mapped_column(ForeignKey("knowledge_indexes.id", ondelete="CASCADE"))
    artifact_id: Mapped[str | None] = mapped_column(ForeignKey("artifacts.id"))
    name: Mapped[str] = mapped_column(String(255))
    status: Mapped[str] = mapped_column(String(30), default="queued", index=True)
    progress: Mapped[int] = mapped_column(default=0)
    lease_token: Mapped[str | None] = mapped_column(String(36))
    lease_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    cancel_requested: Mapped[bool] = mapped_column(default=False)
    error: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    config: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)


class Chunk(Owned, Base):
    """带出处的向量片段；无固定列维度，查询按不可变索引 ID 过滤后计算距离。"""

    __tablename__ = "chunks"
    document_id: Mapped[str] = mapped_column(ForeignKey("documents.id", ondelete="CASCADE"))
    index_id: Mapped[str] = mapped_column(
        ForeignKey("knowledge_indexes.id", ondelete="CASCADE"), index=True
    )
    version: Mapped[int] = mapped_column(Integer)
    position: Mapped[int] = mapped_column(Integer)
    page: Mapped[int | None] = mapped_column(Integer)
    content: Mapped[str] = mapped_column(Text)
    embedding: Mapped[list[float]] = mapped_column(Vector())
    metadata_: Mapped[dict[str, Any]] = mapped_column("metadata", JSONB, default=dict)
    __table_args__ = (UniqueConstraint("document_id", "index_id", "position"),)
