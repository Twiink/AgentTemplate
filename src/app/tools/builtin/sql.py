"""只读 SQL：专用只读连接、只读事务、语句与函数白名单，并校验连接角色没有特权。"""

from __future__ import annotations

import json
from typing import Any

from pydantic import BaseModel, Field
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from app.core.config import ToolConfig
from app.core.errors import AppError, Code
from app.runtime.contracts import current_run


class ReadSQL(BaseModel):
    """查询使用参数绑定；数据库必须为专门只读角色。"""

    query: str = Field(min_length=1, max_length=20000)
    parameters: dict[str, Any] = Field(default_factory=dict)


async def sql_read(config: ToolConfig, query: str, parameters: dict[str, Any]):
    """通过只读事务与非特权只读角色执行单条查询；超时和返回数量均受限制。"""
    if not config.readonly_database_url:
        raise AppError(Code.SQL_NOT_CONFIGURED, "专用只读连接未配置", 503)
    if ";" in query or not query.lstrip().lower().startswith(("select ", "with ")):
        raise AppError(Code.SQL_DENIED, "只接受单条查询", 403)
    import re

    dangerous = re.compile(
        r"(?i)\b(set_config|pg_sleep|dblink|lo_import|lo_export|pg_read_file|pg_write_file|pg_advisory|nextval|setval|pg_terminate_backend|pg_cancel_backend)\w*\s*\("
    )
    if dangerous.search(query):
        raise AppError(Code.SQL_DENIED, "查询包含受限函数", 403)
    import sqlglot
    from sqlglot import exp

    try:
        statements = sqlglot.parse(query, read="postgres")
        if len(statements) != 1 or not isinstance(statements[0], exp.Query):
            raise ValueError("仅查询")
        parsed = statements[0]
        ctes = {cte.alias.lower() for cte in parsed.find_all(exp.CTE)}
        for table in parsed.find_all(exp.Table):
            name = ".".join(part for part in [table.catalog, table.db, table.name] if part).lower()
            if name not in ctes and name not in {v.lower() for v in config.sql_allowed_tables}:
                raise AppError(Code.SQL_TABLE_DENIED, "查询表不在只读允许列表", 403)
        functions = {
            "COUNT",
            "SUM",
            "AVG",
            "MIN",
            "MAX",
            "COALESCE",
            "NULLIF",
            "LOWER",
            "UPPER",
            "LENGTH",
            "CHAR_LENGTH",
            "ROUND",
            "ABS",
            "CAST",
            "TRY_CAST",
            "SUBSTRING",
            "DATE_TRUNC",
            "EXTRACT",
            "CURRENT_DATE",
            "CURRENT_TIMESTAMP",
            "NOW",
            "CONCAT",
            "TRIM",
            "ARRAY_AGG",
            "STRING_AGG",
            "JSON_EXTRACT",
            "JSON_EXTRACT_SCALAR",
        }
        for function in parsed.find_all(exp.Func):
            name = (
                function.name.upper()
                if isinstance(function, exp.Anonymous)
                else function.sql_name()
            )
            if name not in functions:
                raise AppError(Code.SQL_FUNCTION_DENIED, "查询函数不在允许列表", 403)
    except AppError:
        raise
    except Exception as error:
        raise AppError(Code.SQL_DENIED, "查询无法安全解析", 403) from error
    context = current_run.get()
    if context is None:
        raise AppError(Code.RUN_CONTEXT_REQUIRED, "SQL 查询需要可信运行身份", 403)
    engine = create_async_engine(config.readonly_database_url.get_secret_value())
    try:
        async with engine.connect() as connection, connection.begin():
            await connection.execute(text("SET TRANSACTION READ ONLY"))
            await connection.execute(text("SET LOCAL statement_timeout='5s'"))
            await connection.execute(
                text(
                    "SELECT set_config('app.tenant_id',:tenant,true), "
                    "set_config('app.subject_id',:subject,true), set_config('app.system','false',true)"
                ),
                {
                    "tenant": context.principal.tenant_id,
                    "subject": context.principal.subject_id,
                },
            )
            role = (
                await connection.execute(
                    text("SELECT rolsuper, rolbypassrls FROM pg_roles WHERE rolname=current_user")
                )
            ).one()
            if role.rolsuper or role.rolbypassrls:
                raise AppError(Code.SQL_ROLE_DENIED, "只读工具不允许特权数据库角色", 403)
            result = await connection.stream(text(query), parameters)
            rows = []
            async for row in result.mappings():
                rows.append(dict(row))
                if len(rows) >= config.sql_row_limit:
                    break
            await result.close()
            return json.loads(json.dumps(rows, default=str))
    finally:
        await engine.dispose()
