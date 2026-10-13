"""添加 API Key 管理表；支持密钥名称、作用域、过期和吊销。"""

from alembic import op

revision = "core_0002"
down_revision = "core_0001"
branch_labels = None
depends_on = None

STATEMENTS = [
    """
CREATE TABLE api_keys (
	name VARCHAR(255) NOT NULL,
	digest VARCHAR(64) NOT NULL,
	scopes JSONB NOT NULL,
	last_used_at TIMESTAMP WITH TIME ZONE,
	expires_at TIMESTAMP WITH TIME ZONE,
	revoked BOOLEAN NOT NULL,
	id VARCHAR(36) NOT NULL,
	tenant_id VARCHAR(255) NOT NULL,
	subject_id VARCHAR(255) NOT NULL,
	created_at TIMESTAMP WITH TIME ZONE NOT NULL,
	PRIMARY KEY (id),
	UNIQUE (digest)
)
""",
    "CREATE INDEX ix_api_keys_digest ON api_keys (digest)",
    "CREATE INDEX ix_api_keys_owner ON api_keys (tenant_id, subject_id)",
    "CREATE INDEX ix_api_keys_tenant_id ON api_keys (tenant_id)",
    "CREATE INDEX ix_api_keys_subject_id ON api_keys (subject_id)",
    'ALTER TABLE "api_keys" ENABLE ROW LEVEL SECURITY',
    'ALTER TABLE "api_keys" FORCE ROW LEVEL SECURITY',
    """CREATE POLICY owner_scope ON "api_keys" USING ((current_setting('app.system', true) = 'true' OR (tenant_id = current_setting('app.tenant_id', true) AND subject_id = current_setting('app.subject_id', true)))) WITH CHECK ((current_setting('app.system', true) = 'true' OR (tenant_id = current_setting('app.tenant_id', true) AND subject_id = current_setting('app.subject_id', true))))""",
]


def upgrade():
    """创建 api_keys 表并启用行级安全。"""
    for statement in STATEMENTS:
        op.execute(statement)


def downgrade():
    """删除 api_keys 表。"""
    op.execute("DROP TABLE IF EXISTS api_keys CASCADE")
