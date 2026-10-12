"""事务投递表；业务事务内写入，调度进程发布到 Celery 后确认。"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import DateTime, Index, String
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, Owned


class Outbox(Owned, Base):
    """事务投递记录；发布与确认之间可能重复，消费者必须再次核验运行租约。"""

    __tablename__ = "outbox"
    task: Mapped[str] = mapped_column(String(80))
    resource_id: Mapped[str] = mapped_column(String(36), index=True)
    delivered_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    __table_args__ = (
        Index("ix_outbox_pending", "created_at", postgresql_where=delivered_at.is_(None)),
    )
