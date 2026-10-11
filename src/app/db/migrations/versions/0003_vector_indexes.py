"""常见维度的部分向量索引；DDL 仅在迁移执行，运行角色不需要表所有权。"""

from alembic import op

revision = "0003_vector_indexes"
down_revision = "0002_background_recovery"
branch_labels = None
depends_on = None

DIMENSIONS = [384, 512, 768, 1024, 1536, 2000, 3072, 4000]


def upgrade():
    """按维度建立匹配查询表达式的 HNSW；模型空间仍通过 index_id 严格隔离。"""
    for dimensions in DIMENSIONS:
        kind = "vector" if dimensions <= 2000 else "halfvec"
        operator = "vector_cosine_ops" if dimensions <= 2000 else "halfvec_cosine_ops"
        op.execute(
            f"CREATE INDEX chunks_hnsw_{dimensions} ON chunks USING hnsw "
            f"((embedding::{kind}({dimensions})) {operator}) WHERE vector_dims(embedding)={dimensions}"
        )


def downgrade():
    """只删除性能索引，不删除内容和向量。"""
    for dimensions in DIMENSIONS:
        op.execute(f"DROP INDEX chunks_hnsw_{dimensions}")
