"""附件元数据表；对象内容在本地目录或 S3，表里只保存不透明对象键。"""

from __future__ import annotations

from sqlalchemy import Integer, String
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, Owned


class Artifact(Owned, Base):
    """附件元数据；对象键由服务生成，用户提供的文件名仅用于展示。"""

    __tablename__ = "artifacts"
    filename: Mapped[str] = mapped_column(String(255))
    content_type: Mapped[str] = mapped_column(String(150))
    object_key: Mapped[str] = mapped_column(String(255), unique=True)
    sha256: Mapped[str] = mapped_column(String(64))
    size: Mapped[int] = mapped_column(Integer)
