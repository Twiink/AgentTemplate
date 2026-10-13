"""数据库迁移：先诊断已有结构，再按核心与启用模块的分支升级，最后为运行角色授权。"""

from __future__ import annotations

import asyncio

import typer
from alembic import command
from sqlalchemy import text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import create_async_engine

from app.cli.app import cli
from app.core.config import ConfigurationError, Settings, load_settings
from app.core.errors import AppError, Code
from app.db.models import CORE_TABLES
from app.db.revisions import CORE_BRANCH, alembic_config, known_revisions
from app.modules import ModuleError, ModuleInfo, module_tables, resolve

# LangGraph 原生检查点的表；setup 由迁移角色执行，运行角色只获得读写权限。
CHECKPOINT_TABLES = ("checkpoints", "checkpoint_blobs", "checkpoint_writes")


async def inspect_migrations(settings: Settings) -> str:
    """诊断真实数据库表和版本；未登记版本的历史表和不属于本代码版本链的版本都拒绝迁移。"""
    engine = create_async_engine(
        (settings.migration_database_url or settings.database_url).get_secret_value()
    )
    try:
        async with engine.connect() as connection:
            tables = set(
                (
                    await connection.execute(
                        text("SELECT tablename FROM pg_tables WHERE schemaname='public'")
                    )
                ).scalars()
            )
            if not tables:
                return "empty"
            if "alembic_version" not in tables:
                raise AppError(
                    "legacy_database", "发现未登记版本的历史表；请备份并人工迁移，模板未修改数据"
                )
            revisions = list(
                (
                    await connection.execute(text("SELECT version_num FROM alembic_version"))
                ).scalars()
            )
            # 已知版本以磁盘上的迁移脚本为准，项目新增的迁移自动纳入版本链。
            known = known_revisions()
            if not revisions or any(revision not in known for revision in revisions):
                raise AppError(
                    "legacy_database",
                    "数据库版本不属于本代码的迁移版本链；请人工迁移，模板未修改数据",
                )
            return "、".join(sorted(revisions))
    finally:
        await engine.dispose()


async def setup_checkpointer_and_grants(settings: Settings, infos: list[ModuleInfo]) -> None:
    """创建原生检查点结构，并只为核心表、启用模块的表与检查点表向运行角色授权。"""
    from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver

    url = make_url((settings.migration_database_url or settings.database_url).get_secret_value())
    async with AsyncPostgresSaver.from_conn_string(
        url.set(drivername="postgresql").render_as_string(hide_password=False)
    ) as saver:
        await saver.setup()
    runtime_role = make_url(settings.database_url.get_secret_value()).username
    if not runtime_role:
        raise AppError(Code.RUNTIME_ROLE_MISSING, "运行连接必须声明专门角色")
    engine = create_async_engine(
        (settings.migration_database_url or settings.database_url).get_secret_value()
    )
    role_identifier = '"' + runtime_role.replace('"', '""') + '"'
    try:
        async with engine.begin() as connection:
            await connection.execute(text(f"GRANT USAGE ON SCHEMA public TO {role_identifier}"))
            for name in [*CORE_TABLES, *module_tables(infos), *CHECKPOINT_TABLES]:
                await connection.execute(
                    text(
                        f'GRANT SELECT, INSERT, UPDATE, DELETE ON TABLE "{name}" TO {role_identifier}'
                    )
                )
            await connection.execute(
                text(f"GRANT SELECT ON checkpoint_migrations, alembic_version TO {role_identifier}")
            )
    finally:
        await engine.dispose()


@cli.command()
def migrate():
    """先诊断数据库，再升级核心与启用模块的迁移分支、初始化检查点并授权；绝不自动降级或删除数据。"""
    try:
        settings = load_settings()
        infos = resolve(settings.modules)
        state = asyncio.run(inspect_migrations(settings))
        typer.echo(f"迁移前诊断：{state}")
        config = alembic_config()
        # 未启用模块的分支不升级；之后启用模块时再次执行 migrate 即可补齐。
        for branch in [CORE_BRANCH, *(info.name for info in infos)]:
            command.upgrade(config, f"{branch}@head")
        asyncio.run(setup_checkpointer_and_grants(settings, infos))
        typer.echo("迁移与原生 Checkpointer 结构初始化完成。")
    except AppError as error:
        typer.echo(error.message, err=True)
        raise typer.Exit(1) from error
    except (ConfigurationError, ModuleError) as error:
        typer.echo(str(error), err=True)
        raise typer.Exit(1) from None
    except Exception as error:
        typer.echo(
            f"迁移失败：{type(error).__name__}；请检查数据库连接与权限，未自动清理数据。", err=True
        )
        raise typer.Exit(1) from error
