"""为 Memory 添加 embedding 向量字段以支持语义检索。

Revision ID: memory_0002
Revises: memory_0001
"""

from alembic import op

revision = "memory_0002"
down_revision = "memory_0001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """添加 embedding 向量列。"""
    op.execute("ALTER TABLE memories ADD COLUMN embedding vector")


def downgrade() -> None:
    """删除 embedding 列。"""
    op.execute("ALTER TABLE memories DROP COLUMN embedding")
