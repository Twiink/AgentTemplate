"""模板自有持久模型；原生框架检查点由独立生命周期管理，不重复存储会话状态。"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

from pgvector.sqlalchemy import Vector
from sqlalchemy import DateTime, ForeignKey, Index, Integer, String, Text, UniqueConstraint
from sqlalchemy.dialects.postgresql import JSONB
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


class Thread(Owned, Base):
    """会话归属与运行互斥引用；不再维护一份与框架检查点竞争的消息历史。"""

    __tablename__ = "threads"
    title: Mapped[str | None] = mapped_column(String(255))
    active_run: Mapped[str | None] = mapped_column(String(36))


class Run(Owned, Base):
    """运行状态和恢复输入；lease_token 防止过期 Worker 覆盖新的执行结果。"""

    __tablename__ = "runs"
    thread_id: Mapped[str] = mapped_column(ForeignKey("threads.id", ondelete="CASCADE"), index=True)
    agent_id: Mapped[str] = mapped_column(String(255))
    status: Mapped[str] = mapped_column(String(32), default="queued", index=True)
    input: Mapped[dict[str, Any]] = mapped_column(JSONB)
    output: Mapped[Any | None] = mapped_column(JSONB, nullable=True)
    error: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    options: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)
    snapshot: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    pending: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    resume: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    scopes: Mapped[list[str]] = mapped_column(JSONB)
    usage: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)
    trace_context: Mapped[dict[str, str]] = mapped_column(JSONB, default=dict)
    prompt_versions: Mapped[dict[str, str]] = mapped_column(JSONB, default=dict)
    cancel_requested: Mapped[bool] = mapped_column(default=False)
    lease_token: Mapped[str | None] = mapped_column(String(36))
    lease_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    event_sequence: Mapped[int] = mapped_column(default=0)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    __table_args__ = (Index("ix_runs_owner_created", "tenant_id", "subject_id", "created_at"),)


class RunEvent(Owned, Base):
    """有序事件日志；序号在运行行锁内分配，支持可靠回放与断线续传。"""

    __tablename__ = "run_events"
    run_id: Mapped[str] = mapped_column(ForeignKey("runs.id", ondelete="CASCADE"))
    sequence: Mapped[int] = mapped_column(Integer)
    kind: Mapped[str] = mapped_column(String(50))
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB)
    __table_args__ = (UniqueConstraint("run_id", "sequence"),)


class Outbox(Owned, Base):
    """事务投递记录；发布与确认之间可能重复，消费者必须再次核验运行租约。"""

    __tablename__ = "outbox"
    task: Mapped[str] = mapped_column(String(80))
    resource_id: Mapped[str] = mapped_column(String(36), index=True)
    delivered_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    __table_args__ = (
        Index("ix_outbox_pending", "created_at", postgresql_where=delivered_at.is_(None)),
    )


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


class Artifact(Owned, Base):
    """附件元数据；对象键由服务生成，用户提供的文件名仅用于展示。"""

    __tablename__ = "artifacts"
    filename: Mapped[str] = mapped_column(String(255))
    content_type: Mapped[str] = mapped_column(String(150))
    object_key: Mapped[str] = mapped_column(String(255), unique=True)
    sha256: Mapped[str] = mapped_column(String(64))
    size: Mapped[int] = mapped_column(Integer)


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


class ToolCall(Owned, Base):
    """副作用账本；执行前记录，执行后保存结果，未知结果不会自动再执行。"""

    __tablename__ = "tool_calls"
    run_id: Mapped[str] = mapped_column(ForeignKey("runs.id", ondelete="CASCADE"))
    call_id: Mapped[str] = mapped_column(String(255))
    tool: Mapped[str] = mapped_column(String(255))
    arguments_hash: Mapped[str] = mapped_column(String(64))
    status: Mapped[str] = mapped_column(String(30))
    result: Mapped[Any | None] = mapped_column(JSONB, nullable=True)
    __table_args__ = (UniqueConstraint("run_id", "call_id"),)


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
