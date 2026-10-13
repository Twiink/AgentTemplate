"""配置验证与导出命令：检查配置完整性、导出当前配置、生成文档。"""

from __future__ import annotations

from pathlib import Path
from typing import Annotated, Any

import typer
import yaml

from app.cli.app import cli
from app.core.config import load_settings

config_cli = typer.Typer(help="配置管理：验证、导出和文档生成。", no_args_is_help=True)
cli.add_typer(config_cli, name="config")


@config_cli.command("validate")
def validate_config(
    root: Annotated[
        Path | None,
        typer.Option("--root", exists=True, file_okay=False, help="配置根目录"),
    ] = None,
) -> None:
    """验证配置文件完整性和正确性。"""
    try:
        settings = load_settings(root=root)

        typer.echo("配置验证通过")
        typer.echo(f"\n环境: {settings.environment}")
        typer.echo(f"预设: {settings.preset}")
        typer.echo(f"应用名: {settings.app_name}")
        typer.echo("\n配置来源:")
        for source in settings.config_sources:
            typer.echo(f"  - {source}")

        typer.echo(f"\n启用的模块 ({len(settings.modules)}):")
        for module in settings.modules:
            typer.echo(f"  - {module}")

        if settings.agents.enabled:
            typer.echo(f"\n启用的 Agent ({len(settings.agents.enabled)}):")
            for agent in settings.agents.enabled:
                typer.echo(f"  - {agent}")

    except Exception as error:
        typer.echo(f"配置验证失败: {error}", err=True)
        raise typer.Exit(1) from error


@config_cli.command("export")
def export_config(
    root: Annotated[
        Path | None,
        typer.Option("--root", exists=True, file_okay=False, help="配置根目录"),
    ] = None,
    output: Annotated[Path | None, typer.Option("--output", "-o", help="输出文件路径")] = None,
    include_secrets: Annotated[
        bool, typer.Option("--include-secrets", help="包含敏感信息（谨慎使用）")
    ] = False,
) -> None:
    """导出合并后的完整配置。"""
    try:
        settings = load_settings(root=root)

        if include_secrets:
            data = settings.model_dump(mode="json")
        else:
            data = settings.model_dump(mode="json", exclude={"config_sources"})
            _redact_secrets(data)

        yaml_output = yaml.safe_dump(
            data, allow_unicode=True, default_flow_style=False, sort_keys=False
        )

        if output:
            output.write_text(yaml_output, encoding="utf-8")
            typer.echo(f"配置已导出到: {output}")
        else:
            typer.echo(yaml_output)

        if not include_secrets:
            typer.echo(
                "\n敏感字段已隐藏，使用 --include-secrets 导出完整配置（谨慎使用）",
                err=True,
            )

    except Exception as error:
        typer.echo(f"配置导出失败: {error}", err=True)
        raise typer.Exit(1) from error


def _redact_secrets(data: dict[str, Any]) -> None:
    """递归隐藏敏感字段。"""
    secret_keys = {
        "database_url",
        "migration_database_url",
        "redis_url",
        "api_key",
        "secret",
        "token",
        "password",
        "dsn",
        "digest",
    }

    for key, value in list(data.items()):
        if any(secret in key.lower() for secret in secret_keys):
            if value:
                data[key] = "***REDACTED***"
        elif isinstance(value, dict):
            _redact_secrets(value)
        elif isinstance(value, list):
            for item in value:
                if isinstance(item, dict):
                    _redact_secrets(item)


@config_cli.command("schema")
def export_schema(
    output: Annotated[Path | None, typer.Option("--output", "-o", help="输出文件路径")] = None,
) -> None:
    """导出配置的 JSON Schema，用于 IDE 自动补全和验证。"""
    from app.core.config import Settings

    schema = Settings.model_json_schema()
    properties = schema.get("properties")
    if isinstance(properties, dict):
        properties.pop("config_sources", None)

    yaml_output = yaml.safe_dump(
        schema, allow_unicode=True, default_flow_style=False, sort_keys=False
    )

    if output:
        output.write_text(yaml_output, encoding="utf-8")
        typer.echo(f"Schema 已导出到: {output}")
    else:
        typer.echo(yaml_output)
