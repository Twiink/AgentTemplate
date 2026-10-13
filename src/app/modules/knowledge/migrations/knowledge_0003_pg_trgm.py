"""启用 pg_trgm 扩展并为 Chunk.content 添加 GIN 索引以支持关键词检索。

Revision ID: knowledge_0003
Revises: knowledge_0002
"""

from alembic import op

revision = "knowledge_0003"
down_revision = "knowledge_0002"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """启用 pg_trgm 并创建 GIN 索引。"""
    op.execute("CREATE EXTENSION IF NOT EXISTS pg_trgm")
    op.execute(
        """
        CREATE INDEX IF NOT EXISTS ix_chunk_content_trgm 
        ON chunk USING GIN (content gin_trgm_ops)
        """
    )


def downgrade() -> None:
    """删除 GIN 索引，保留扩展。"""
    op.execute("DROP INDEX IF EXISTS ix_chunk_content_trgm")
