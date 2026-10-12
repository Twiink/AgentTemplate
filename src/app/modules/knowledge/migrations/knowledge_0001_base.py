"""knowledge 模块初始结构：pgvector 扩展、知识库、向量空间、文档、导入任务与向量片段；导入任务关联附件表。"""

from alembic import op

revision = "knowledge_0001"
down_revision = None
branch_labels = ("knowledge",)
depends_on = ("storage_0001",)

STATEMENTS = [
    "CREATE EXTENSION IF NOT EXISTS vector",
    "\nCREATE TABLE knowledge_bases (\n\tname VARCHAR(255) NOT NULL, \n\tactive_index VARCHAR(36), \n\tid VARCHAR(36) NOT NULL, \n\ttenant_id VARCHAR(255) NOT NULL, \n\tsubject_id VARCHAR(255) NOT NULL, \n\tcreated_at TIMESTAMP WITH TIME ZONE NOT NULL, \n\tPRIMARY KEY (id)\n)\n\n",
    "CREATE INDEX ix_knowledge_bases_subject_id ON knowledge_bases (subject_id)",
    "CREATE INDEX ix_knowledge_bases_tenant_id ON knowledge_bases (tenant_id)",
    'ALTER TABLE "knowledge_bases" ENABLE ROW LEVEL SECURITY',
    'ALTER TABLE "knowledge_bases" FORCE ROW LEVEL SECURITY',
    "CREATE POLICY owner_scope ON \"knowledge_bases\" USING ((current_setting('app.system', true) = 'true' OR (tenant_id = current_setting('app.tenant_id', true) AND subject_id = current_setting('app.subject_id', true)))) WITH CHECK ((current_setting('app.system', true) = 'true' OR (tenant_id = current_setting('app.tenant_id', true) AND subject_id = current_setting('app.subject_id', true))))",
    "\nCREATE TABLE knowledge_indexes (\n\tknowledge_id VARCHAR(36) NOT NULL, \n\tprofile JSONB NOT NULL, \n\tstatus VARCHAR(30) NOT NULL, \n\tid VARCHAR(36) NOT NULL, \n\ttenant_id VARCHAR(255) NOT NULL, \n\tsubject_id VARCHAR(255) NOT NULL, \n\tcreated_at TIMESTAMP WITH TIME ZONE NOT NULL, \n\tPRIMARY KEY (id), \n\tFOREIGN KEY(knowledge_id) REFERENCES knowledge_bases (id) ON DELETE CASCADE\n)\n\n",
    "CREATE INDEX ix_knowledge_indexes_subject_id ON knowledge_indexes (subject_id)",
    "CREATE INDEX ix_knowledge_indexes_tenant_id ON knowledge_indexes (tenant_id)",
    'ALTER TABLE "knowledge_indexes" ENABLE ROW LEVEL SECURITY',
    'ALTER TABLE "knowledge_indexes" FORCE ROW LEVEL SECURITY',
    "CREATE POLICY owner_scope ON \"knowledge_indexes\" USING ((current_setting('app.system', true) = 'true' OR (tenant_id = current_setting('app.tenant_id', true) AND subject_id = current_setting('app.subject_id', true)))) WITH CHECK ((current_setting('app.system', true) = 'true' OR (tenant_id = current_setting('app.tenant_id', true) AND subject_id = current_setting('app.subject_id', true))))",
    "\nCREATE TABLE documents (\n\tknowledge_id VARCHAR(36) NOT NULL, \n\tname VARCHAR(255) NOT NULL, \n\tversion INTEGER NOT NULL, \n\tid VARCHAR(36) NOT NULL, \n\ttenant_id VARCHAR(255) NOT NULL, \n\tsubject_id VARCHAR(255) NOT NULL, \n\tcreated_at TIMESTAMP WITH TIME ZONE NOT NULL, \n\tPRIMARY KEY (id), \n\tUNIQUE (knowledge_id, name), \n\tFOREIGN KEY(knowledge_id) REFERENCES knowledge_bases (id) ON DELETE CASCADE\n)\n\n",
    "CREATE INDEX ix_documents_tenant_id ON documents (tenant_id)",
    "CREATE INDEX ix_documents_subject_id ON documents (subject_id)",
    'ALTER TABLE "documents" ENABLE ROW LEVEL SECURITY',
    'ALTER TABLE "documents" FORCE ROW LEVEL SECURITY',
    "CREATE POLICY owner_scope ON \"documents\" USING ((current_setting('app.system', true) = 'true' OR (tenant_id = current_setting('app.tenant_id', true) AND subject_id = current_setting('app.subject_id', true)))) WITH CHECK ((current_setting('app.system', true) = 'true' OR (tenant_id = current_setting('app.tenant_id', true) AND subject_id = current_setting('app.subject_id', true))))",
    "\nCREATE TABLE import_jobs (\n\tknowledge_id VARCHAR(36) NOT NULL, \n\tindex_id VARCHAR(36) NOT NULL, \n\tartifact_id VARCHAR(36), \n\tname VARCHAR(255) NOT NULL, \n\tstatus VARCHAR(30) NOT NULL, \n\tprogress INTEGER NOT NULL, \n\tcancel_requested BOOLEAN NOT NULL, \n\terror JSONB, \n\tconfig JSONB NOT NULL, \n\tlease_token VARCHAR(36), \n\tlease_until TIMESTAMP WITH TIME ZONE, \n\tid VARCHAR(36) NOT NULL, \n\ttenant_id VARCHAR(255) NOT NULL, \n\tsubject_id VARCHAR(255) NOT NULL, \n\tcreated_at TIMESTAMP WITH TIME ZONE NOT NULL, \n\tPRIMARY KEY (id), \n\tFOREIGN KEY(knowledge_id) REFERENCES knowledge_bases (id) ON DELETE CASCADE, \n\tFOREIGN KEY(index_id) REFERENCES knowledge_indexes (id) ON DELETE CASCADE, \n\tFOREIGN KEY(artifact_id) REFERENCES artifacts (id)\n)\n\n",
    "CREATE INDEX ix_import_jobs_subject_id ON import_jobs (subject_id)",
    "CREATE INDEX ix_import_jobs_status ON import_jobs (status)",
    "CREATE INDEX ix_import_jobs_tenant_id ON import_jobs (tenant_id)",
    'ALTER TABLE "import_jobs" ENABLE ROW LEVEL SECURITY',
    'ALTER TABLE "import_jobs" FORCE ROW LEVEL SECURITY',
    "CREATE POLICY owner_scope ON \"import_jobs\" USING ((current_setting('app.system', true) = 'true' OR (tenant_id = current_setting('app.tenant_id', true) AND subject_id = current_setting('app.subject_id', true)))) WITH CHECK ((current_setting('app.system', true) = 'true' OR (tenant_id = current_setting('app.tenant_id', true) AND subject_id = current_setting('app.subject_id', true))))",
    "\nCREATE TABLE chunks (\n\tdocument_id VARCHAR(36) NOT NULL, \n\tindex_id VARCHAR(36) NOT NULL, \n\tversion INTEGER NOT NULL, \n\tposition INTEGER NOT NULL, \n\tpage INTEGER, \n\tcontent TEXT NOT NULL, \n\tembedding VECTOR NOT NULL, \n\tmetadata JSONB NOT NULL, \n\tid VARCHAR(36) NOT NULL, \n\ttenant_id VARCHAR(255) NOT NULL, \n\tsubject_id VARCHAR(255) NOT NULL, \n\tcreated_at TIMESTAMP WITH TIME ZONE NOT NULL, \n\tPRIMARY KEY (id), \n\tUNIQUE (document_id, index_id, position), \n\tFOREIGN KEY(document_id) REFERENCES documents (id) ON DELETE CASCADE, \n\tFOREIGN KEY(index_id) REFERENCES knowledge_indexes (id) ON DELETE CASCADE\n)\n\n",
    "CREATE INDEX ix_chunks_subject_id ON chunks (subject_id)",
    "CREATE INDEX ix_chunks_index_id ON chunks (index_id)",
    "CREATE INDEX ix_chunks_tenant_id ON chunks (tenant_id)",
    'ALTER TABLE "chunks" ENABLE ROW LEVEL SECURITY',
    'ALTER TABLE "chunks" FORCE ROW LEVEL SECURITY',
    "CREATE POLICY owner_scope ON \"chunks\" USING ((current_setting('app.system', true) = 'true' OR (tenant_id = current_setting('app.tenant_id', true) AND subject_id = current_setting('app.subject_id', true)))) WITH CHECK ((current_setting('app.system', true) = 'true' OR (tenant_id = current_setting('app.tenant_id', true) AND subject_id = current_setting('app.subject_id', true))))",
]


def upgrade():
    """依次创建表、索引并启用强制行级安全。"""
    for statement in STATEMENTS:
        op.execute(statement)


def downgrade():
    """初始结构不支持自动删除数据；回退应使用经过验证的备份恢复。"""
    raise RuntimeError("初始迁移不支持自动删除数据")
