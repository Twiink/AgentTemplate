"""迁移中使用的行级安全辅助函数：为带 tenant_id、subject_id 的表启用强制 RLS 并创建归属策略。

基线迁移保存固定 DDL，不依赖本函数；项目新增的业务表在迁移中调用 owner_policy。本函数的输出
已被迁移使用，修改时只能新增函数，不能改变已有函数的语句。
"""

from __future__ import annotations

# 与基线迁移一致：内部调度事务可见全部数据，其余事务只能访问当前租户与主体的数据。
OWNER_PREDICATE = (
    "(current_setting('app.system', true) = 'true' OR "
    "(tenant_id = current_setting('app.tenant_id', true) "
    "AND subject_id = current_setting('app.subject_id', true)))"
)


def owner_policy(table: str) -> list[str]:
    """返回启用强制行级安全并创建归属策略的语句；表必须包含 tenant_id 与 subject_id 列。

    用法：在迁移的 upgrade 中执行 for statement in owner_policy("orders"): op.execute(statement)，
    之后执行 agent-template migrate 时运行角色会自动获得该表的读写授权；表名需登记在
    app.db.models.CORE_TABLES 或所属模块的 tables 中。
    """
    quoted = '"' + table.replace('"', '""') + '"'
    return [
        f"ALTER TABLE {quoted} ENABLE ROW LEVEL SECURITY",
        f"ALTER TABLE {quoted} FORCE ROW LEVEL SECURITY",
        f"CREATE POLICY owner_scope ON {quoted} USING {OWNER_PREDICATE} "
        f"WITH CHECK {OWNER_PREDICATE}",
    ]
