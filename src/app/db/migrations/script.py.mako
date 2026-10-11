"""${message}

版本：${up_revision}
前置版本：${down_revision | comma,n}
创建日期：${create_date}
"""
from alembic import op
import sqlalchemy as sa
${imports if imports else ""}

revision = ${repr(up_revision)}
down_revision = ${repr(down_revision)}
branch_labels = ${repr(branch_labels)}
depends_on = ${repr(depends_on)}


def upgrade() -> None:
    """应用当前版本；生成后核对数据兼容性与锁持有时间。"""
    ${upgrades if upgrades else "pass"}


def downgrade() -> None:
    """回退前必须核对备份与数据保留，禁止未经验证自动清空。"""
    ${downgrades if downgrades else "pass"}
