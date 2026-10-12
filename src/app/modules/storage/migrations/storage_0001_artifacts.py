"""storage 模块初始结构：附件元数据表。"""

from alembic import op

revision = "storage_0001"
down_revision = None
branch_labels = ("storage",)
depends_on = None

STATEMENTS = [
    "\nCREATE TABLE artifacts (\n\tfilename VARCHAR(255) NOT NULL, \n\tcontent_type VARCHAR(150) NOT NULL, \n\tobject_key VARCHAR(255) NOT NULL, \n\tsha256 VARCHAR(64) NOT NULL, \n\tsize INTEGER NOT NULL, \n\tid VARCHAR(36) NOT NULL, \n\ttenant_id VARCHAR(255) NOT NULL, \n\tsubject_id VARCHAR(255) NOT NULL, \n\tcreated_at TIMESTAMP WITH TIME ZONE NOT NULL, \n\tPRIMARY KEY (id), \n\tUNIQUE (object_key)\n)\n\n",
    "CREATE INDEX ix_artifacts_subject_id ON artifacts (subject_id)",
    "CREATE INDEX ix_artifacts_tenant_id ON artifacts (tenant_id)",
    'ALTER TABLE "artifacts" ENABLE ROW LEVEL SECURITY',
    'ALTER TABLE "artifacts" FORCE ROW LEVEL SECURITY',
    "CREATE POLICY owner_scope ON \"artifacts\" USING ((current_setting('app.system', true) = 'true' OR (tenant_id = current_setting('app.tenant_id', true) AND subject_id = current_setting('app.subject_id', true)))) WITH CHECK ((current_setting('app.system', true) = 'true' OR (tenant_id = current_setting('app.tenant_id', true) AND subject_id = current_setting('app.subject_id', true))))",
]


def upgrade():
    """依次创建表、索引并启用强制行级安全。"""
    for statement in STATEMENTS:
        op.execute(statement)


def downgrade():
    """初始结构不支持自动删除数据；回退应使用经过验证的备份恢复。"""
    raise RuntimeError("初始迁移不支持自动删除数据")
