"""项目初始化：选择预设，生成私有配置与开发凭据，可选删除未启用模块的代码与部署文件。"""

from __future__ import annotations

import hashlib
import importlib.metadata
import secrets
import shutil
from pathlib import Path
from typing import Annotated
from urllib.parse import quote

import typer
import yaml

from app.cli.app import cli
from app.cli.common import private_write, settings_or_exit
from app.core.config import PRESETS
from app.modules import CATALOG

# minimal 不使用这些部署文件；--prune 时一并删除。
FULL_ONLY_PATHS = (
    "compose.full.yaml",
    "compose.observability.yaml",
    "deploy/kubernetes",
    "deploy/observability",
)


def preset_modules(preset: str) -> list[str]:
    """读取预设文件中的默认模块列表。"""
    path = Path(f"config/presets/{preset}.yaml")
    if not path.exists():
        raise typer.BadParameter(f"预设文件不存在：{path}")
    content = yaml.safe_load(path.read_text()) or {}
    return list(content.get("modules", []))


def prune_targets(preset: str) -> list[Path]:
    """列出 --prune 会删除的路径：未启用模块的代码与迁移，minimal 还包括 full 专用的部署文件。"""
    enabled = set(preset_modules(preset))
    targets = [info.path for name, info in CATALOG.items() if name not in enabled and info.present]
    if preset == "minimal":
        targets += [Path(item) for item in FULL_ONLY_PATHS]
    return [path for path in targets if path.exists()]


def prune(preset: str, assume_yes: bool) -> None:
    """删除未启用的模块与部署文件；只删除文件，不改动数据库，删除前列出路径并确认。"""
    targets = prune_targets(preset)
    if not targets:
        typer.echo("没有需要删除的文件。")
        return
    typer.echo("将删除以下路径：")
    for path in targets:
        typer.echo(f"  {path}")
    if not assume_yes and not typer.confirm("确认删除？", default=False):
        typer.echo("已取消删除。")
        return
    for path in targets:
        if path.is_dir():
            shutil.rmtree(path)
        else:
            path.unlink()
    typer.echo("已删除。pyproject.toml 中的 extras 定义保留，不影响安装。")


@cli.command()
def init(
    preset: Annotated[str, typer.Option(help="预设：minimal 或 full")] = "full",
    prune_files: Annotated[
        bool, typer.Option("--prune", help="删除未启用模块的代码、迁移与部署文件")
    ] = False,
    assume_yes: Annotated[bool, typer.Option("--yes", "-y", help="删除文件前不再确认")] = False,
):
    """选择预设并初始化开发目录、私有凭据和配置；不连接或清空数据库。"""
    if preset not in PRESETS:
        raise typer.BadParameter("预设只能是 minimal 或 full")
    for package in ["fastapi", "langchain", "langgraph"]:
        importlib.metadata.version(package)
    for directory in [".data/artifacts", ".data/workspace", ".data/metrics", "prompts", "config"]:
        Path(directory).mkdir(parents=True, exist_ok=True)
    private = Path("config/private.yaml")
    if private.exists():
        current = settings_or_exit().preset
        typer.echo(f"私有配置已存在，保留原文件；当前预设为 {current}。")
        if current != preset:
            typer.echo("如需切换预设，请修改 config/private.yaml 中的 preset 或 modules。")
    else:
        if Path(".data/compose.env").exists() or Path(".data/developer.key").exists():
            raise typer.BadParameter("发现部分初始化文件，请先核对其来源再补全 private.yaml")
        password = secrets.token_urlsafe(32)
        token = secrets.token_urlsafe(40)
        encoded = quote(password, safe="")
        private_write(Path(".data/developer.key"), token + "\n")
        # AGENT_EXTRAS 决定 Compose 构建镜像时安装哪些模块依赖；minimal 为空，只装核心依赖。
        extras = "full" if preset == "full" else ""
        private_write(
            Path(".data/compose.env"),
            f"AGENT_DB_PASSWORD={password}\nAGENT_EXTRAS={extras}\n",
        )
        config = {
            "preset": preset,
            "database_url": f"postgresql+asyncpg://agent_app:{encoded}@localhost:5433/agenttemplate",
            "migration_database_url": (
                f"postgresql+asyncpg://agent_owner:{encoded}@localhost:5433/agenttemplate"
            ),
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
        typer.echo(
            f"初始化完成，预设为 {preset}。私有凭据在 .data/developer.key，配置在 config/private.yaml。"
        )
    if prune_files:
        prune(preset, assume_yes)
    typer.echo("下一步：")
    typer.echo("  " + ("uv sync --extra full" if preset == "full" else "uv sync"))
    typer.echo("  docker compose --env-file .data/compose.env up -d postgres redis")
    typer.echo("  uv run agent-template migrate")
    typer.echo("  uv run agent-template preflight")
    typer.echo("  uv run agent-template dev")
    if preset == "full":
        typer.echo("  uv run agent-template worker")
        typer.echo("  uv run agent-template scheduler")
    # 复用模板时只提示重新设置身份，不自动改变现有项目绑定。
    typer.echo("新项目请重新设置 .openviking/config.json 的 peer.id。")
