"""项目初始化：选择预设，生成私有配置与开发凭据，可选删除未启用模块的代码与部署文件。"""

from __future__ import annotations

import hashlib
import importlib.metadata
import json
import os
import re
import secrets
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Annotated
from urllib.parse import quote

import typer
import yaml

from app.cli.app import cli
from app.cli.common import private_write, settings_or_exit
from app.core.config import PRESETS
from app.modules import CATALOG

# minimal 不使用 Worker 和 Kubernetes 文件；共享观测栈保留为可选能力。
FULL_ONLY_PATHS = ("compose.full.yaml", "deploy/kubernetes")
TEMPLATE_PEER_ID = "github.com-twiink-agenttemplate"
TEMPLATE_REMOTE = "https://github.com/twiink/agenttemplate"


def project_slug(name: str) -> str:
    """将显示名转换为 PostgreSQL 与 Compose 可复用的稳定项目标识。"""
    slug = re.sub(r"[^a-z0-9]+", "-", name.strip().lower()).strip("-")
    if not slug:
        slug = "agent-" + secrets.token_hex(4)
    if slug[0].isdigit():
        slug = "agent-" + slug
    if len(slug) > 48:
        raise typer.BadParameter("项目名称转换后的标识不能超过 48 个字符")
    return slug


def normalized_remote(value: str) -> str:
    """规范化常见 GitHub HTTPS/SSH 地址，避免在上游模板仓库轮换 peer。"""
    value = value.strip().lower()
    if value.startswith("git@github.com:"):
        value = "https://github.com/" + value.removeprefix("git@github.com:")
    return value.removesuffix(".git").rstrip("/")


def refresh_project_peer(config_path: Path, origin: str) -> bool:
    """仅当派生仓库仍携带模板默认 peer 时生成项目专属身份。"""
    if normalized_remote(origin) == TEMPLATE_REMOTE or not config_path.is_file():
        return False
    try:
        config = json.loads(config_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise typer.BadParameter(f"无法读取 {config_path}：{type(error).__name__}") from None
    peer = config.get("peer")
    if not isinstance(peer, dict) or peer.get("id") != TEMPLATE_PEER_ID:
        return False
    peer["id"] = "project-" + secrets.token_hex(12)
    content = json.dumps(config, ensure_ascii=False, indent=2) + "\n"
    descriptor, temporary_name = tempfile.mkstemp(
        prefix="config-", suffix=".json", dir=config_path.parent
    )
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(content)
        os.replace(temporary_name, config_path)
    finally:
        if os.path.exists(temporary_name):
            os.unlink(temporary_name)
    return True


def current_origin() -> str:
    """读取当前仓库 origin；无 Git remote 时返回空字符串。"""
    try:
        result = subprocess.run(
            ["git", "remote", "get-url", "origin"],
            check=True,
            capture_output=True,
            text=True,
        )
    except (OSError, subprocess.CalledProcessError):
        return ""
    return result.stdout.strip()


def preset_modules(preset: str) -> list[str]:
    """读取预设文件中的默认模块列表。"""
    path = Path(f"config/presets/{preset}.yaml")
    if not path.exists():
        raise typer.BadParameter(f"预设文件不存在：{path}")
    content = yaml.safe_load(path.read_text()) or {}
    return list(content.get("modules", []))


def prune_targets(preset: str) -> list[Path]:
    """列出 --prune 会删除的路径；共享观测部署文件不随 minimal 档移除。"""
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
    preset: Annotated[str, typer.Option(help="预设：minimal 或 full")] = "minimal",
    project_name: Annotated[
        str | None, typer.Option("--project-name", help="新项目显示名称")
    ] = None,
    prune_files: Annotated[
        bool, typer.Option("--prune", help="删除未启用模块的代码、迁移与部署文件")
    ] = False,
    assume_yes: Annotated[bool, typer.Option("--yes", "-y", help="删除文件前不再确认")] = False,
):
    """选择预设并初始化开发目录、私有凭据和配置；不连接或清空数据库。"""
    if preset not in PRESETS:
        raise typer.BadParameter("预设只能是 minimal 或 full")
    display_name = project_name.strip() if project_name else Path.cwd().name
    database_name = project_slug(display_name)
    if refresh_project_peer(Path(".openviking/config.json"), current_origin()):
        typer.echo("检测到派生仓库仍使用模板 peer；已生成独立项目身份。")
    for package in ["fastapi", "langchain", "langgraph"]:
        importlib.metadata.version(package)
    for directory in [".data/artifacts", ".data/workspace", ".data/metrics", "prompts", "config"]:
        Path(directory).mkdir(parents=True, exist_ok=True)
    private = Path("config/private.yaml")
    if private.exists():
        current = settings_or_exit()
        typer.echo(f"私有配置已存在，保留原文件；当前预设为 {current.preset}。")
        if project_name and current.app_name != display_name:
            typer.echo("已有配置保留；本次 --project-name 未应用。")
        if current.preset != preset:
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
            f"COMPOSE_PROJECT_NAME={database_name}\nAGENT_DB_NAME={database_name}\n"
            f"AGENT_DB_PASSWORD={password}\nAGENT_EXTRAS={extras}\n",
        )
        config = {
            "preset": preset,
            "app_name": display_name,
            "database_url": f"postgresql+asyncpg://agent_app:{encoded}@localhost:5433/{database_name}",
            "migration_database_url": (
                f"postgresql+asyncpg://agent_owner:{encoded}@localhost:5433/{database_name}"
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
    if not private.exists():
        typer.echo(f"项目名称：{display_name}；Compose/数据库标识：{database_name}。")
    typer.echo("现有私有配置与显式 OpenViking peer 保留不变。")
