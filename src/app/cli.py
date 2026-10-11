"""面向真实开发的命令入口；初始化幂等，迁移诊断历史结构，秘密只写入私有文件。"""

from __future__ import annotations

import asyncio
import hashlib
import importlib.metadata
import json
import os
import secrets
import subprocess
import sys
from pathlib import Path
from urllib.parse import quote

import typer
import yaml
from alembic import command
from alembic.config import Config
from sqlalchemy import text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import create_async_engine

from app.core.config import load_settings
from app.core.errors import AppError
from app.core.resources import Resources
from app.services.health import diagnostics

cli = typer.Typer(
    help="AgentTemplate 初始化、诊断、开发与交付命令",
    no_args_is_help=True,
    pretty_exceptions_enable=False,
)


def private_write(path: Path, content: str):
    """用排他创建和 0600 权限写入私有文件；并发或重复初始化不会覆盖内容。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(descriptor, "w") as handle:
            handle.write(content)
    except FileExistsError:
        return False
    return True


@cli.command()
def init():
    """检查依赖并初始化真实开发目录、私有凭据和配置；不连接或清空数据库。"""
    for package in ["fastapi", "langchain", "langgraph", "llama-index-core", "celery"]:
        importlib.metadata.version(package)
    for directory in [".data/artifacts", ".data/workspace", ".data/metrics", "prompts", "config"]:
        Path(directory).mkdir(parents=True, exist_ok=True)
    private = Path("config/private.yaml")
    if private.exists():
        typer.echo("私有配置已存在；保留原文件。")
        load_settings()
        return
    if Path(".data/compose.env").exists() or Path(".data/developer.key").exists():
        raise typer.BadParameter("发现部分初始化文件，请先核对其来源再补全 private.yaml")
    password = secrets.token_urlsafe(32)
    token = secrets.token_urlsafe(40)
    encoded = quote(password, safe="")
    private_write(Path(".data/developer.key"), token + "\n")
    private_write(Path(".data/compose.env"), "AGENT_DB_PASSWORD=" + password + "\n")
    config = {
        "database_url": f"postgresql+asyncpg://agent_app:{encoded}@localhost:5433/agenttemplate",
        "migration_database_url": f"postgresql+asyncpg://agent_owner:{encoded}@localhost:5433/agenttemplate",
        "redis_url": "redis://localhost:6380/0",
        "auth": {
            "api_keys": [
                {
                    "digest": hashlib.sha256(token.encode()).hexdigest(),
                    "tenant_id": "local",
                    "subject_id": "developer",
                    "scopes": [
                        "resources:read",
                        "resources:write",
                        "tools:files:read",
                        "tools:files:write",
                        "tools:http",
                        "tools:search",
                        "tools:sql",
                    ],
                }
            ]
        },
    }
    private_write(private, yaml.safe_dump(config, allow_unicode=True, sort_keys=False))
    typer.echo("初始化完成。私有凭据保存在 .data/developer.key，配置保存在 config/private.yaml。")
    typer.echo("基础设施启动命令：docker compose --env-file .data/compose.env up -d postgres redis")
    # 复用模板时仅提示重新设置身份，不自动改变现有项目绑定。
    typer.echo("新项目请重新设置 .openviking/config.json 的 peer.id。")


@cli.command()
def doctor():
    """检查依赖、配置、真实基础设施、模型声明与 MCP；没有收费模型调用。"""

    async def run():
        """在独立资源上下文中收集诊断，结束后释放已打开连接。"""
        resources = Resources(load_settings())
        try:
            async with resources:
                return await diagnostics(resources, detailed=True)
        finally:
            await resources.stack.aclose()

    result = asyncio.run(run())
    typer.echo(json.dumps(result, ensure_ascii=False, indent=2))
    if not result["ready"]:
        raise typer.Exit(1)


async def inspect_migrations():
    """诊断真实数据库表和版本；历史结构与未知版本拒绝套用新模板初始迁移。"""
    settings = load_settings()
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
            if len(revisions) != 1 or revisions[0] not in {
                "0001_engineering_base",
                "0002_background_recovery",
                "0003_vector_indexes",
            }:
                raise AppError(
                    "legacy_database", "数据库版本不属于新模板版本链；请人工迁移，模板未修改数据"
                )
            return revisions[0]
    finally:
        await engine.dispose()


@cli.command()
def migrate():
    """先诊断数据库，再执行自有迁移和原生检查点 setup；绝不自动降级或删除数据。"""
    try:
        state = asyncio.run(inspect_migrations())
        typer.echo(f"迁移前诊断：{state}")
        config = Config(str(Path(__file__).parent / "db/migrations/alembic.ini"))
        command.upgrade(config, "head")

        async def setup():
            """进入原生 PostgreSQL Saver 上下文并创建检查点结构。"""
            from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver

            settings = load_settings()
            url = make_url(
                (settings.migration_database_url or settings.database_url).get_secret_value()
            ).set(drivername="postgresql")
            async with AsyncPostgresSaver.from_conn_string(
                url.render_as_string(hide_password=False)
            ) as saver:
                await saver.setup()
            from app.db.models import Base

            runtime_role = make_url(settings.database_url.get_secret_value()).username
            if not runtime_role:
                raise AppError("runtime_role_missing", "运行连接必须声明专门角色")
            # 迁移角色只为模板及原生检查点表授权，不对整个数据库开放所有表。
            engine = create_async_engine(
                (settings.migration_database_url or settings.database_url).get_secret_value()
            )
            role_identifier = '"' + runtime_role.replace('"', '""') + '"'
            try:
                async with engine.begin() as connection:
                    await connection.execute(
                        text(f"GRANT USAGE ON SCHEMA public TO {role_identifier}")
                    )
                    for name in [
                        *Base.metadata.tables,
                        "checkpoints",
                        "checkpoint_blobs",
                        "checkpoint_writes",
                    ]:
                        await connection.execute(
                            text(
                                f'GRANT SELECT, INSERT, UPDATE, DELETE ON TABLE "{name}" TO {role_identifier}'
                            )
                        )
                    await connection.execute(
                        text(
                            f"GRANT SELECT ON checkpoint_migrations, alembic_version TO {role_identifier}"
                        )
                    )
            finally:
                await engine.dispose()

        asyncio.run(setup())
        typer.echo("模板迁移与原生 Checkpointer 结构初始化完成。")
    except AppError as error:
        typer.echo(error.message, err=True)
        raise typer.Exit(1) from error
    except Exception as error:
        typer.echo(
            f"迁移失败：{type(error).__name__}；请检查数据库连接与权限，未自动清理数据。", err=True
        )
        raise typer.Exit(1) from error


@cli.command()
def dev(host: str = "127.0.0.1", port: int = 8000):
    """启动 FastAPI 开发进程，保留重载能力。"""
    import uvicorn

    uvicorn.run("app.main:app", host=host, port=port, reload=True)


@cli.command()
def worker(concurrency: int = 2):
    """启动真实 Celery Worker，使用正确模块入口。"""
    import os

    if os.getenv("PROMETHEUS_MULTIPROC_DIR"):
        from prometheus_client import CollectorRegistry, multiprocess, start_http_server

        registry = CollectorRegistry()
        multiprocess.MultiProcessCollector(registry)
        start_http_server(9005, registry=registry)
    from app.worker.celery import celery_app

    celery_app.worker_main(["worker", "--loglevel=INFO", f"--concurrency={concurrency}"])


@cli.command()
def scheduler():
    """启动任务投递、租约恢复与保留期清理调度；只部署一个 beat 实例。"""
    from app.worker.celery import celery_app

    celery_app.start(["beat", "--loglevel=INFO", "--schedule=.data/celerybeat"])


@cli.command()
def check(integration: bool = False):
    """执行真实格式、lint、类型、规范与测试检查；任意失败返回非零。"""
    commands = [
        ["uv", "run", "ruff", "format", "--check", "src", "tests", "scripts"],
        ["uv", "run", "ruff", "check", "src", "tests", "scripts"],
        ["uv", "run", "ty", "check", "src"],
        [sys.executable, "scripts/check_conventions.py"],
        [
            "uv",
            "run",
            "pytest",
            "-q",
            "-m",
            "integration" if integration else "not integration and not providers",
        ],
    ]
    for invocation in commands:
        result = subprocess.run(invocation, check=False)
        if result.returncode:
            raise typer.Exit(result.returncode)


@cli.command()
def evaluate(
    dataset: Path,
    scorer: str,
    output: Path,
    baseline: Path | None = None,
    threshold: float | None = None,
):
    """读取开发者真实数据集和评分器，输出本次运行与回归结果。"""
    from app.services.evaluation import evaluate_dataset

    asyncio.run(evaluate_dataset(dataset, scorer, output, baseline, threshold))


@cli.command("observability-init")
def observability_init(langfuse: bool = True, langsmith: bool = False):
    """为选择的真实观测目标生成私有部署配置，LangSmith Key 由操作者填入。"""
    from app.observability_cli import initialize_observability

    initialize_observability(langfuse, langsmith)
    typer.echo("观测私有配置已生成；目标组合保存在 .data/collector.yaml。")
