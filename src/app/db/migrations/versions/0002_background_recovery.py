"""文档租约与可选记忆提取结构；已有底座数据原地升级，不覆盖原始记录。"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision = "0002_background_recovery"
down_revision = "0001_engineering_base"
branch_labels = None
depends_on = None


def upgrade():
    """增加导入租约与来源唯一任务，启用相同的可信身份策略。"""
    op.add_column("import_jobs", sa.Column("lease_token", sa.String(36), nullable=True))
    op.add_column(
        "import_jobs", sa.Column("lease_until", sa.DateTime(timezone=True), nullable=True)
    )
    op.create_table(
        "memory_extractions",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("tenant_id", sa.String(255), nullable=False),
        sa.Column("subject_id", sa.String(255), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "source_run_id",
            sa.String(36),
            sa.ForeignKey("runs.id", ondelete="CASCADE"),
            unique=True,
            nullable=False,
        ),
        sa.Column("extraction_run_id", sa.String(36)),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column("attempts", sa.Integer(), nullable=False),
        sa.Column("error", JSONB),
    )
    op.create_index("ix_memory_extractions_tenant_id", "memory_extractions", ["tenant_id"])
    op.create_index("ix_memory_extractions_subject_id", "memory_extractions", ["subject_id"])
    op.execute("ALTER TABLE memory_extractions ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE memory_extractions FORCE ROW LEVEL SECURITY")
    predicate = "(current_setting('app.system',true)='true' OR (tenant_id=current_setting('app.tenant_id',true) AND subject_id=current_setting('app.subject_id',true)))"
    op.execute(
        f"CREATE POLICY owner_scope ON memory_extractions USING ({predicate}) WITH CHECK ({predicate})"
    )


def downgrade():
    """自动回退会丢失租约和任务状态，必须使用备份恢复。"""
    raise RuntimeError("后台恢复结构不支持自动删除数据")
