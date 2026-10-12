"""background 模块初始结构：事务投递 outbox 表。"""

from alembic import op

revision = "background_0001"
down_revision = None
branch_labels = ("background",)
depends_on = None

STATEMENTS = [
    "\nCREATE TABLE outbox (\n\ttask VARCHAR(80) NOT NULL, \n\tresource_id VARCHAR(36) NOT NULL, \n\tdelivered_at TIMESTAMP WITH TIME ZONE, \n\tid VARCHAR(36) NOT NULL, \n\ttenant_id VARCHAR(255) NOT NULL, \n\tsubject_id VARCHAR(255) NOT NULL, \n\tcreated_at TIMESTAMP WITH TIME ZONE NOT NULL, \n\tPRIMARY KEY (id)\n)\n\n",
    "CREATE INDEX ix_outbox_tenant_id ON outbox (tenant_id)",
    "CREATE INDEX ix_outbox_pending ON outbox (created_at) WHERE delivered_at IS NULL",
    "CREATE INDEX ix_outbox_subject_id ON outbox (subject_id)",
    "CREATE INDEX ix_outbox_resource_id ON outbox (resource_id)",
    'ALTER TABLE "outbox" ENABLE ROW LEVEL SECURITY',
    'ALTER TABLE "outbox" FORCE ROW LEVEL SECURITY',
    "CREATE POLICY owner_scope ON \"outbox\" USING ((current_setting('app.system', true) = 'true' OR (tenant_id = current_setting('app.tenant_id', true) AND subject_id = current_setting('app.subject_id', true)))) WITH CHECK ((current_setting('app.system', true) = 'true' OR (tenant_id = current_setting('app.tenant_id', true) AND subject_id = current_setting('app.subject_id', true))))",
]


def upgrade():
    """依次创建表、索引并启用强制行级安全。"""
    for statement in STATEMENTS:
        op.execute(statement)


def downgrade():
    """初始结构不支持自动删除数据；回退应使用经过验证的备份恢复。"""
    raise RuntimeError("初始迁移不支持自动删除数据")
