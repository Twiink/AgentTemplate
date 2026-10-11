"""不可变工程底座初始结构；迁移保存固定 DDL，不随未来 ORM 修改而改变。"""

from alembic import op

revision = "0001_engineering_base"
down_revision = None
branch_labels = None
depends_on = None

STATEMENTS = [
    "CREATE EXTENSION IF NOT EXISTS vector",
    "\nCREATE TABLE artifacts (\n\tfilename VARCHAR(255) NOT NULL, \n\tcontent_type VARCHAR(150) NOT NULL, \n\tobject_key VARCHAR(255) NOT NULL, \n\tsha256 VARCHAR(64) NOT NULL, \n\tsize INTEGER NOT NULL, \n\tid VARCHAR(36) NOT NULL, \n\ttenant_id VARCHAR(255) NOT NULL, \n\tsubject_id VARCHAR(255) NOT NULL, \n\tcreated_at TIMESTAMP WITH TIME ZONE NOT NULL, \n\tPRIMARY KEY (id), \n\tUNIQUE (object_key)\n)\n\n",
    "CREATE INDEX ix_artifacts_subject_id ON artifacts (subject_id)",
    "CREATE INDEX ix_artifacts_tenant_id ON artifacts (tenant_id)",
    'ALTER TABLE "artifacts" ENABLE ROW LEVEL SECURITY',
    'ALTER TABLE "artifacts" FORCE ROW LEVEL SECURITY',
    "CREATE POLICY owner_scope ON \"artifacts\" USING ((current_setting('app.system', true) = 'true' OR (tenant_id = current_setting('app.tenant_id', true) AND subject_id = current_setting('app.subject_id', true)))) WITH CHECK ((current_setting('app.system', true) = 'true' OR (tenant_id = current_setting('app.tenant_id', true) AND subject_id = current_setting('app.subject_id', true))))",
    "\nCREATE TABLE knowledge_bases (\n\tname VARCHAR(255) NOT NULL, \n\tactive_index VARCHAR(36), \n\tid VARCHAR(36) NOT NULL, \n\ttenant_id VARCHAR(255) NOT NULL, \n\tsubject_id VARCHAR(255) NOT NULL, \n\tcreated_at TIMESTAMP WITH TIME ZONE NOT NULL, \n\tPRIMARY KEY (id)\n)\n\n",
    "CREATE INDEX ix_knowledge_bases_subject_id ON knowledge_bases (subject_id)",
    "CREATE INDEX ix_knowledge_bases_tenant_id ON knowledge_bases (tenant_id)",
    'ALTER TABLE "knowledge_bases" ENABLE ROW LEVEL SECURITY',
    'ALTER TABLE "knowledge_bases" FORCE ROW LEVEL SECURITY',
    "CREATE POLICY owner_scope ON \"knowledge_bases\" USING ((current_setting('app.system', true) = 'true' OR (tenant_id = current_setting('app.tenant_id', true) AND subject_id = current_setting('app.subject_id', true)))) WITH CHECK ((current_setting('app.system', true) = 'true' OR (tenant_id = current_setting('app.tenant_id', true) AND subject_id = current_setting('app.subject_id', true))))",
    "\nCREATE TABLE memories (\n\tnamespace VARCHAR(255) NOT NULL, \n\tkey VARCHAR(255) NOT NULL, \n\tcontent TEXT NOT NULL, \n\tsources JSONB NOT NULL, \n\tmetadata JSONB NOT NULL, \n\tversion INTEGER NOT NULL, \n\tupdated_at TIMESTAMP WITH TIME ZONE NOT NULL, \n\texpires_at TIMESTAMP WITH TIME ZONE, \n\tid VARCHAR(36) NOT NULL, \n\ttenant_id VARCHAR(255) NOT NULL, \n\tsubject_id VARCHAR(255) NOT NULL, \n\tcreated_at TIMESTAMP WITH TIME ZONE NOT NULL, \n\tPRIMARY KEY (id), \n\tUNIQUE (tenant_id, subject_id, namespace, key)\n)\n\n",
    "CREATE INDEX ix_memories_tenant_id ON memories (tenant_id)",
    "CREATE INDEX ix_memories_subject_id ON memories (subject_id)",
    "CREATE INDEX ix_memories_namespace ON memories (namespace)",
    'ALTER TABLE "memories" ENABLE ROW LEVEL SECURITY',
    'ALTER TABLE "memories" FORCE ROW LEVEL SECURITY',
    "CREATE POLICY owner_scope ON \"memories\" USING ((current_setting('app.system', true) = 'true' OR (tenant_id = current_setting('app.tenant_id', true) AND subject_id = current_setting('app.subject_id', true)))) WITH CHECK ((current_setting('app.system', true) = 'true' OR (tenant_id = current_setting('app.tenant_id', true) AND subject_id = current_setting('app.subject_id', true))))",
    "\nCREATE TABLE outbox (\n\ttask VARCHAR(80) NOT NULL, \n\tresource_id VARCHAR(36) NOT NULL, \n\tdelivered_at TIMESTAMP WITH TIME ZONE, \n\tid VARCHAR(36) NOT NULL, \n\ttenant_id VARCHAR(255) NOT NULL, \n\tsubject_id VARCHAR(255) NOT NULL, \n\tcreated_at TIMESTAMP WITH TIME ZONE NOT NULL, \n\tPRIMARY KEY (id)\n)\n\n",
    "CREATE INDEX ix_outbox_tenant_id ON outbox (tenant_id)",
    "CREATE INDEX ix_outbox_pending ON outbox (created_at) WHERE delivered_at IS NULL",
    "CREATE INDEX ix_outbox_subject_id ON outbox (subject_id)",
    "CREATE INDEX ix_outbox_resource_id ON outbox (resource_id)",
    'ALTER TABLE "outbox" ENABLE ROW LEVEL SECURITY',
    'ALTER TABLE "outbox" FORCE ROW LEVEL SECURITY',
    "CREATE POLICY owner_scope ON \"outbox\" USING ((current_setting('app.system', true) = 'true' OR (tenant_id = current_setting('app.tenant_id', true) AND subject_id = current_setting('app.subject_id', true)))) WITH CHECK ((current_setting('app.system', true) = 'true' OR (tenant_id = current_setting('app.tenant_id', true) AND subject_id = current_setting('app.subject_id', true))))",
    "\nCREATE TABLE threads (\n\ttitle VARCHAR(255), \n\tactive_run VARCHAR(36), \n\tid VARCHAR(36) NOT NULL, \n\ttenant_id VARCHAR(255) NOT NULL, \n\tsubject_id VARCHAR(255) NOT NULL, \n\tcreated_at TIMESTAMP WITH TIME ZONE NOT NULL, \n\tPRIMARY KEY (id)\n)\n\n",
    "CREATE INDEX ix_threads_subject_id ON threads (subject_id)",
    "CREATE INDEX ix_threads_tenant_id ON threads (tenant_id)",
    'ALTER TABLE "threads" ENABLE ROW LEVEL SECURITY',
    'ALTER TABLE "threads" FORCE ROW LEVEL SECURITY',
    "CREATE POLICY owner_scope ON \"threads\" USING ((current_setting('app.system', true) = 'true' OR (tenant_id = current_setting('app.tenant_id', true) AND subject_id = current_setting('app.subject_id', true)))) WITH CHECK ((current_setting('app.system', true) = 'true' OR (tenant_id = current_setting('app.tenant_id', true) AND subject_id = current_setting('app.subject_id', true))))",
    "\nCREATE TABLE documents (\n\tknowledge_id VARCHAR(36) NOT NULL, \n\tname VARCHAR(255) NOT NULL, \n\tversion INTEGER NOT NULL, \n\tid VARCHAR(36) NOT NULL, \n\ttenant_id VARCHAR(255) NOT NULL, \n\tsubject_id VARCHAR(255) NOT NULL, \n\tcreated_at TIMESTAMP WITH TIME ZONE NOT NULL, \n\tPRIMARY KEY (id), \n\tUNIQUE (knowledge_id, name), \n\tFOREIGN KEY(knowledge_id) REFERENCES knowledge_bases (id) ON DELETE CASCADE\n)\n\n",
    "CREATE INDEX ix_documents_tenant_id ON documents (tenant_id)",
    "CREATE INDEX ix_documents_subject_id ON documents (subject_id)",
    'ALTER TABLE "documents" ENABLE ROW LEVEL SECURITY',
    'ALTER TABLE "documents" FORCE ROW LEVEL SECURITY',
    "CREATE POLICY owner_scope ON \"documents\" USING ((current_setting('app.system', true) = 'true' OR (tenant_id = current_setting('app.tenant_id', true) AND subject_id = current_setting('app.subject_id', true)))) WITH CHECK ((current_setting('app.system', true) = 'true' OR (tenant_id = current_setting('app.tenant_id', true) AND subject_id = current_setting('app.subject_id', true))))",
    "\nCREATE TABLE knowledge_indexes (\n\tknowledge_id VARCHAR(36) NOT NULL, \n\tprofile JSONB NOT NULL, \n\tstatus VARCHAR(30) NOT NULL, \n\tid VARCHAR(36) NOT NULL, \n\ttenant_id VARCHAR(255) NOT NULL, \n\tsubject_id VARCHAR(255) NOT NULL, \n\tcreated_at TIMESTAMP WITH TIME ZONE NOT NULL, \n\tPRIMARY KEY (id), \n\tFOREIGN KEY(knowledge_id) REFERENCES knowledge_bases (id) ON DELETE CASCADE\n)\n\n",
    "CREATE INDEX ix_knowledge_indexes_subject_id ON knowledge_indexes (subject_id)",
    "CREATE INDEX ix_knowledge_indexes_tenant_id ON knowledge_indexes (tenant_id)",
    'ALTER TABLE "knowledge_indexes" ENABLE ROW LEVEL SECURITY',
    'ALTER TABLE "knowledge_indexes" FORCE ROW LEVEL SECURITY',
    "CREATE POLICY owner_scope ON \"knowledge_indexes\" USING ((current_setting('app.system', true) = 'true' OR (tenant_id = current_setting('app.tenant_id', true) AND subject_id = current_setting('app.subject_id', true)))) WITH CHECK ((current_setting('app.system', true) = 'true' OR (tenant_id = current_setting('app.tenant_id', true) AND subject_id = current_setting('app.subject_id', true))))",
    "\nCREATE TABLE runs (\n\tthread_id VARCHAR(36) NOT NULL, \n\tagent_id VARCHAR(255) NOT NULL, \n\tstatus VARCHAR(32) NOT NULL, \n\tinput JSONB NOT NULL, \n\toutput JSONB, \n\terror JSONB, \n\toptions JSONB NOT NULL, \n\tsnapshot JSONB, \n\tpending JSONB, \n\tresume JSONB, \n\tscopes JSONB NOT NULL, \n\tusage JSONB NOT NULL, \n\ttrace_context JSONB NOT NULL, \n\tprompt_versions JSONB NOT NULL, \n\tcancel_requested BOOLEAN NOT NULL, \n\tlease_token VARCHAR(36), \n\tlease_until TIMESTAMP WITH TIME ZONE, \n\tevent_sequence INTEGER NOT NULL, \n\tcompleted_at TIMESTAMP WITH TIME ZONE, \n\tid VARCHAR(36) NOT NULL, \n\ttenant_id VARCHAR(255) NOT NULL, \n\tsubject_id VARCHAR(255) NOT NULL, \n\tcreated_at TIMESTAMP WITH TIME ZONE NOT NULL, \n\tPRIMARY KEY (id), \n\tFOREIGN KEY(thread_id) REFERENCES threads (id) ON DELETE CASCADE\n)\n\n",
    "CREATE INDEX ix_runs_thread_id ON runs (thread_id)",
    "CREATE INDEX ix_runs_owner_created ON runs (tenant_id, subject_id, created_at)",
    "CREATE INDEX ix_runs_tenant_id ON runs (tenant_id)",
    "CREATE INDEX ix_runs_subject_id ON runs (subject_id)",
    "CREATE INDEX ix_runs_status ON runs (status)",
    'ALTER TABLE "runs" ENABLE ROW LEVEL SECURITY',
    'ALTER TABLE "runs" FORCE ROW LEVEL SECURITY',
    "CREATE POLICY owner_scope ON \"runs\" USING ((current_setting('app.system', true) = 'true' OR (tenant_id = current_setting('app.tenant_id', true) AND subject_id = current_setting('app.subject_id', true)))) WITH CHECK ((current_setting('app.system', true) = 'true' OR (tenant_id = current_setting('app.tenant_id', true) AND subject_id = current_setting('app.subject_id', true))))",
    "\nCREATE TABLE chunks (\n\tdocument_id VARCHAR(36) NOT NULL, \n\tindex_id VARCHAR(36) NOT NULL, \n\tversion INTEGER NOT NULL, \n\tposition INTEGER NOT NULL, \n\tpage INTEGER, \n\tcontent TEXT NOT NULL, \n\tembedding VECTOR NOT NULL, \n\tmetadata JSONB NOT NULL, \n\tid VARCHAR(36) NOT NULL, \n\ttenant_id VARCHAR(255) NOT NULL, \n\tsubject_id VARCHAR(255) NOT NULL, \n\tcreated_at TIMESTAMP WITH TIME ZONE NOT NULL, \n\tPRIMARY KEY (id), \n\tUNIQUE (document_id, index_id, position), \n\tFOREIGN KEY(document_id) REFERENCES documents (id) ON DELETE CASCADE, \n\tFOREIGN KEY(index_id) REFERENCES knowledge_indexes (id) ON DELETE CASCADE\n)\n\n",
    "CREATE INDEX ix_chunks_subject_id ON chunks (subject_id)",
    "CREATE INDEX ix_chunks_index_id ON chunks (index_id)",
    "CREATE INDEX ix_chunks_tenant_id ON chunks (tenant_id)",
    'ALTER TABLE "chunks" ENABLE ROW LEVEL SECURITY',
    'ALTER TABLE "chunks" FORCE ROW LEVEL SECURITY',
    "CREATE POLICY owner_scope ON \"chunks\" USING ((current_setting('app.system', true) = 'true' OR (tenant_id = current_setting('app.tenant_id', true) AND subject_id = current_setting('app.subject_id', true)))) WITH CHECK ((current_setting('app.system', true) = 'true' OR (tenant_id = current_setting('app.tenant_id', true) AND subject_id = current_setting('app.subject_id', true))))",
    "\nCREATE TABLE import_jobs (\n\tknowledge_id VARCHAR(36) NOT NULL, \n\tindex_id VARCHAR(36) NOT NULL, \n\tartifact_id VARCHAR(36), \n\tname VARCHAR(255) NOT NULL, \n\tstatus VARCHAR(30) NOT NULL, \n\tprogress INTEGER NOT NULL, \n\tcancel_requested BOOLEAN NOT NULL, \n\terror JSONB, \n\tconfig JSONB NOT NULL, \n\tid VARCHAR(36) NOT NULL, \n\ttenant_id VARCHAR(255) NOT NULL, \n\tsubject_id VARCHAR(255) NOT NULL, \n\tcreated_at TIMESTAMP WITH TIME ZONE NOT NULL, \n\tPRIMARY KEY (id), \n\tFOREIGN KEY(knowledge_id) REFERENCES knowledge_bases (id) ON DELETE CASCADE, \n\tFOREIGN KEY(index_id) REFERENCES knowledge_indexes (id) ON DELETE CASCADE, \n\tFOREIGN KEY(artifact_id) REFERENCES artifacts (id)\n)\n\n",
    "CREATE INDEX ix_import_jobs_subject_id ON import_jobs (subject_id)",
    "CREATE INDEX ix_import_jobs_status ON import_jobs (status)",
    "CREATE INDEX ix_import_jobs_tenant_id ON import_jobs (tenant_id)",
    'ALTER TABLE "import_jobs" ENABLE ROW LEVEL SECURITY',
    'ALTER TABLE "import_jobs" FORCE ROW LEVEL SECURITY',
    "CREATE POLICY owner_scope ON \"import_jobs\" USING ((current_setting('app.system', true) = 'true' OR (tenant_id = current_setting('app.tenant_id', true) AND subject_id = current_setting('app.subject_id', true)))) WITH CHECK ((current_setting('app.system', true) = 'true' OR (tenant_id = current_setting('app.tenant_id', true) AND subject_id = current_setting('app.subject_id', true))))",
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
    """依次创建扩展、表、索引及强制 RLS；历史数据库已由 CLI 预先诊断。"""
    for statement in STATEMENTS:
        op.execute(statement)


def downgrade():
    """本模板禁止自动清空初始数据；回退应采用经验证的备份恢复方案。"""
    raise RuntimeError("初始迁移不支持自动删除数据")
