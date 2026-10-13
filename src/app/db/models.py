"""核心表：线程、运行、运行事件和工具副作用账本；各模块的表在 app.modules.<名称>.models 中定义。"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import DateTime, ForeignKey, Index, Integer, String, UniqueConstraint
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, Owned, identifier, now

__all__ = ["Base", "Owned", "Run", "RunEvent", "Thread", "ToolCall", "ApiKey", "identifier", "now"]

# 核心表名；迁移授权与启动预检的行级安全检查按此清单执行，模块表由各模块声明。
CORE_TABLES = ("threads", "runs", "run_events", "tool_calls", "api_keys")


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


class ApiKey(Owned, Base):
    """API Key 管理；存储 SHA-256 摘要，不保存明文密钥，支持吊销和过期。"""

    __tablename__ = "api_keys"
    name: Mapped[str] = mapped_column(String(255))
    digest: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    scopes: Mapped[list[str]] = mapped_column(JSONB)
    last_used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    revoked: Mapped[bool] = mapped_column(default=False)
    __table_args__ = (Index("ix_api_keys_owner", "tenant_id", "subject_id"),)
