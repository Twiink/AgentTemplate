"""检查命令：启动预检、详细诊断，以及格式、lint、类型与规范的静态检查。"""

from __future__ import annotations

import asyncio
import json
import subprocess
import sys

import typer

from app.cli.app import cli
from app.cli.common import settings_or_exit
from app.core.container import Container
from app.core.preflight import run_preflight
from app.services.health import diagnostics


@cli.command()
def preflight(
    as_json: bool = typer.Option(False, "--json", help="以 JSON 输出结果，供 CI 使用"),
):
    """执行启动预检：配置、模块、目录、数据库、迁移与 Redis；存在失败的阻断项时返回非零。"""
    settings = settings_or_exit()
    report = asyncio.run(run_preflight(settings))
    if as_json:
        typer.echo(json.dumps(report.as_dict(), ensure_ascii=False, indent=2))
    else:
        typer.echo(report.render())
    if not report.ok:
        raise typer.Exit(1)


@cli.command()
def doctor():
    """先执行启动预检，再检查 Trace、Agent、MCP 与各模块状态；不调用收费模型。"""
    settings = settings_or_exit()
    report = asyncio.run(run_preflight(settings))
    typer.echo(report.render())
    if not report.ok:
        raise typer.Exit(1)

    async def run():
        """在独立资源上下文中收集诊断，结束时释放已打开的连接。"""
        async with Container(settings) as container:
            return await diagnostics(container, report)

    result = asyncio.run(run())
    typer.echo(json.dumps(result["components"], ensure_ascii=False, indent=2))


@cli.command()
def check():
    """执行格式、lint、类型与规范检查；任意一项失败返回非零。模板不包含测试代码。"""
    commands = [
        ["uv", "run", "ruff", "format", "--check", "src", "scripts"],
        ["uv", "run", "ruff", "check", "src", "scripts"],
        ["uv", "run", "ty", "check", "src"],
        [sys.executable, "scripts/check_conventions.py"],
    ]
    for invocation in commands:
        result = subprocess.run(invocation, check=False)
        if result.returncode:
            raise typer.Exit(result.returncode)
