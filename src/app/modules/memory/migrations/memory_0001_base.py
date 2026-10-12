"""memory 模块初始结构：长期记忆与记忆提取任务；提取任务关联核心运行表。"""

from alembic import op

revision = "memory_0001"
down_revision = None
branch_labels = ("memory",)
depends_on = ("core_0001",)

STATEMENTS = [
    "\nCREATE TABLE memories (\n\tnamespace VARCHAR(255) NOT NULL, \n\tkey VARCHAR(255) NOT NULL, \n\tcontent TEXT NOT NULL, \n\tsources JSONB NOT NULL, \n\tmetadata JSONB NOT NULL, \n\tversion INTEGER NOT NULL, \n\tupdated_at TIMESTAMP WITH TIME ZONE NOT NULL, \n\texpires_at TIMESTAMP WITH TIME ZONE, \n\tid VARCHAR(36) NOT NULL, \n\ttenant_id VARCHAR(255) NOT NULL, \n\tsubject_id VARCHAR(255) NOT NULL, \n\tcreated_at TIMESTAMP WITH TIME ZONE NOT NULL, \n\tPRIMARY KEY (id), \n\tUNIQUE (tenant_id, subject_id, namespace, key)\n)\n\n",
    "CREATE INDEX ix_memories_tenant_id ON memories (tenant_id)",
    "CREATE INDEX ix_memories_subject_id ON memories (subject_id)",
    "CREATE INDEX ix_memories_namespace ON memories (namespace)",
    'ALTER TABLE "memories" ENABLE ROW LEVEL SECURITY',
    'ALTER TABLE "memories" FORCE ROW LEVEL SECURITY',
    "CREATE POLICY owner_scope ON \"memories\" USING ((current_setting('app.system', true) = 'true' OR (tenant_id = current_setting('app.tenant_id', true) AND subject_id = current_setting('app.subject_id', true)))) WITH CHECK ((current_setting('app.system', true) = 'true' OR (tenant_id = current_setting('app.tenant_id', true) AND subject_id = current_setting('app.subject_id', true))))",
    "\nCREATE TABLE memory_extractions (\n\tsource_run_id VARCHAR(36) NOT NULL, \n\textraction_run_id VARCHAR(36), \n\tstatus VARCHAR(32) NOT NULL, \n\tattempts INTEGER NOT NULL, \n\terror JSONB, \n\tid VARCHAR(36) NOT NULL, \n\ttenant_id VARCHAR(255) NOT NULL, \n\tsubject_id VARCHAR(255) NOT NULL, \n\tcreated_at TIMESTAMP WITH TIME ZONE NOT NULL, \n\tPRIMARY KEY (id), \n\tUNIQUE (source_run_id), \n\tFOREIGN KEY(source_run_id) REFERENCES runs (id) ON DELETE CASCADE\n)\n\n",
    "CREATE INDEX ix_memory_extractions_tenant_id ON memory_extractions (tenant_id)",
    "CREATE INDEX ix_memory_extractions_subject_id ON memory_extractions (subject_id)",
    'ALTER TABLE "memory_extractions" ENABLE ROW LEVEL SECURITY',
    'ALTER TABLE "memory_extractions" FORCE ROW LEVEL SECURITY',
    "CREATE POLICY owner_scope ON \"memory_extractions\" USING ((current_setting('app.system', true) = 'true' OR (tenant_id = current_setting('app.tenant_id', true) AND subject_id = current_setting('app.subject_id', true)))) WITH CHECK ((current_setting('app.system', true) = 'true' OR (tenant_id = current_setting('app.tenant_id', true) AND subject_id = current_setting('app.subject_id', true))))",
]


def upgrade():
    """依次创建表、索引并启用强制行级安全。"""
    for statement in STATEMENTS:
        op.execute(statement)


def downgrade():
    """初始结构不支持自动删除数据；回退应使用经过验证的备份恢复。"""
    raise RuntimeError("初始迁移不支持自动删除数据")
