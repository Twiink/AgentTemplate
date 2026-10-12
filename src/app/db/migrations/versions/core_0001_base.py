"""核心表初始结构：线程、运行、运行事件与工具副作用账本；迁移保存固定 DDL，不随 ORM 修改而改变。"""

from alembic import op

revision = "core_0001"
down_revision = None
branch_labels = ("core",)
depends_on = None

STATEMENTS = [
    "\nCREATE TABLE threads (\n\ttitle VARCHAR(255), \n\tactive_run VARCHAR(36), \n\tid VARCHAR(36) NOT NULL, \n\ttenant_id VARCHAR(255) NOT NULL, \n\tsubject_id VARCHAR(255) NOT NULL, \n\tcreated_at TIMESTAMP WITH TIME ZONE NOT NULL, \n\tPRIMARY KEY (id)\n)\n\n",
    "CREATE INDEX ix_threads_subject_id ON threads (subject_id)",
    "CREATE INDEX ix_threads_tenant_id ON threads (tenant_id)",
    'ALTER TABLE "threads" ENABLE ROW LEVEL SECURITY',
    'ALTER TABLE "threads" FORCE ROW LEVEL SECURITY',
    "CREATE POLICY owner_scope ON \"threads\" USING ((current_setting('app.system', true) = 'true' OR (tenant_id = current_setting('app.tenant_id', true) AND subject_id = current_setting('app.subject_id', true)))) WITH CHECK ((current_setting('app.system', true) = 'true' OR (tenant_id = current_setting('app.tenant_id', true) AND subject_id = current_setting('app.subject_id', true))))",
    "\nCREATE TABLE runs (\n\tthread_id VARCHAR(36) NOT NULL, \n\tagent_id VARCHAR(255) NOT NULL, \n\tstatus VARCHAR(32) NOT NULL, \n\tinput JSONB NOT NULL, \n\toutput JSONB, \n\terror JSONB, \n\toptions JSONB NOT NULL, \n\tsnapshot JSONB, \n\tpending JSONB, \n\tresume JSONB, \n\tscopes JSONB NOT NULL, \n\tusage JSONB NOT NULL, \n\ttrace_context JSONB NOT NULL, \n\tprompt_versions JSONB NOT NULL, \n\tcancel_requested BOOLEAN NOT NULL, \n\tlease_token VARCHAR(36), \n\tlease_until TIMESTAMP WITH TIME ZONE, \n\tevent_sequence INTEGER NOT NULL, \n\tcompleted_at TIMESTAMP WITH TIME ZONE, \n\tid VARCHAR(36) NOT NULL, \n\ttenant_id VARCHAR(255) NOT NULL, \n\tsubject_id VARCHAR(255) NOT NULL, \n\tcreated_at TIMESTAMP WITH TIME ZONE NOT NULL, \n\tPRIMARY KEY (id), \n\tFOREIGN KEY(thread_id) REFERENCES threads (id) ON DELETE CASCADE\n)\n\n",
    "CREATE INDEX ix_runs_thread_id ON runs (thread_id)",
    "CREATE INDEX ix_runs_owner_created ON runs (tenant_id, subject_id, created_at)",
    "CREATE INDEX ix_runs_tenant_id ON runs (tenant_id)",
    "CREATE INDEX ix_runs_subject_id ON runs (subject_id)",
    "CREATE INDEX ix_runs_status ON runs (status)",
    'ALTER TABLE "runs" ENABLE ROW LEVEL SECURITY',
    'ALTER TABLE "runs" FORCE ROW LEVEL SECURITY',
    "CREATE POLICY owner_scope ON \"runs\" USING ((current_setting('app.system', true) = 'true' OR (tenant_id = current_setting('app.tenant_id', true) AND subject_id = current_setting('app.subject_id', true)))) WITH CHECK ((current_setting('app.system', true) = 'true' OR (tenant_id = current_setting('app.tenant_id', true) AND subject_id = current_setting('app.subject_id', true))))",
    "\nCREATE TABLE run_events (\n\trun_id VARCHAR(36) NOT NULL, \n\tsequence INTEGER NOT NULL, \n\tkind VARCHAR(50) NOT NULL, \n\tpayload JSONB NOT NULL, \n\tid VARCHAR(36) NOT NULL, \n\ttenant_id VARCHAR(255) NOT NULL, \n\tsubject_id VARCHAR(255) NOT NULL, \n\tcreated_at TIMESTAMP WITH TIME ZONE NOT NULL, \n\tPRIMARY KEY (id), \n\tUNIQUE (run_id, sequence), \n\tFOREIGN KEY(run_id) REFERENCES runs (id) ON DELETE CASCADE\n)\n\n",
    "CREATE INDEX ix_run_events_tenant_id ON run_events (tenant_id)",
    "CREATE INDEX ix_run_events_subject_id ON run_events (subject_id)",
    'ALTER TABLE "run_events" ENABLE ROW LEVEL SECURITY',
    'ALTER TABLE "run_events" FORCE ROW LEVEL SECURITY',
    "CREATE POLICY owner_scope ON \"run_events\" USING ((current_setting('app.system', true) = 'true' OR (tenant_id = current_setting('app.tenant_id', true) AND subject_id = current_setting('app.subject_id', true)))) WITH CHECK ((current_setting('app.system', true) = 'true' OR (tenant_id = current_setting('app.tenant_id', true) AND subject_id = current_setting('app.subject_id', true))))",
    "\nCREATE TABLE tool_calls (\n\trun_id VARCHAR(36) NOT NULL, \n\tcall_id VARCHAR(255) NOT NULL, \n\ttool VARCHAR(255) NOT NULL, \n\targuments_hash VARCHAR(64) NOT NULL, \n\tstatus VARCHAR(30) NOT NULL, \n\tresult JSONB, \n\tid VARCHAR(36) NOT NULL, \n\ttenant_id VARCHAR(255) NOT NULL, \n\tsubject_id VARCHAR(255) NOT NULL, \n\tcreated_at TIMESTAMP WITH TIME ZONE NOT NULL, \n\tPRIMARY KEY (id), \n\tUNIQUE (run_id, call_id), \n\tFOREIGN KEY(run_id) REFERENCES runs (id) ON DELETE CASCADE\n)\n\n",
    "CREATE INDEX ix_tool_calls_tenant_id ON tool_calls (tenant_id)",
    "CREATE INDEX ix_tool_calls_subject_id ON tool_calls (subject_id)",
    'ALTER TABLE "tool_calls" ENABLE ROW LEVEL SECURITY',
    'ALTER TABLE "tool_calls" FORCE ROW LEVEL SECURITY',
    "CREATE POLICY owner_scope ON \"tool_calls\" USING ((current_setting('app.system', true) = 'true' OR (tenant_id = current_setting('app.tenant_id', true) AND subject_id = current_setting('app.subject_id', true)))) WITH CHECK ((current_setting('app.system', true) = 'true' OR (tenant_id = current_setting('app.tenant_id', true) AND subject_id = current_setting('app.subject_id', true))))",
]


def upgrade():
    """依次创建表、索引并启用强制行级安全。"""
    for statement in STATEMENTS:
        op.execute(statement)


def downgrade():
    """初始结构不支持自动删除数据；回退应使用经过验证的备份恢复。"""
    raise RuntimeError("初始迁移不支持自动删除数据")
