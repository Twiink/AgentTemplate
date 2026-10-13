"""配置验证与导出命令：检查配置完整性、导出当前配置、生成文档。"""

import sys
from pathlib import Path

import click
import yaml

from app.cli.app import cli
from app.core.config import load_settings


@cli.group("config")
def config_group():
    """配置管理：验证、导出和文档生成。"""
    pass


@config_group.command("validate")
@click.option("--root", type=click.Path(exists=True), help="配置根目录")
def validate_config(root: str | None):
    """验证配置文件完整性和正确性。"""
    try:
        root_path = Path(root) if root else None
        settings = load_settings(root=root_path)

        click.echo("✓ 配置验证通过")
        click.echo(f"\n环境: {settings.environment}")
        click.echo(f"预设: {settings.preset}")
        click.echo(f"应用名: {settings.app_name}")
        click.echo("\n配置来源:")
        for source in settings.config_sources:
            click.echo(f"  - {source}")

        click.echo(f"\n启用的模块 ({len(settings.modules)}):")
        for module in settings.modules:
            click.echo(f"  - {module}")

        if settings.agents.enabled:
            click.echo(f"\n启用的 Agent ({len(settings.agents.enabled)}):")
            for agent in settings.agents.enabled:
                click.echo(f"  - {agent}")

    except Exception as error:
        click.echo(f"❌ 配置验证失败: {error}", err=True)
        sys.exit(1)


@config_group.command("export")
@click.option("--root", type=click.Path(exists=True), help="配置根目录")
@click.option("--output", "-o", type=click.Path(), help="输出文件路径")
@click.option("--include-secrets", is_flag=True, help="包含敏感信息（谨慎使用）")
def export_config(root: str | None, output: str | None, include_secrets: bool):
    """导出合并后的完整配置。"""
    try:
        root_path = Path(root) if root else None
        settings = load_settings(root=root_path)

        # 导出为字典
        if include_secrets:
            data = settings.model_dump(mode="json")
        else:
            # 不包含敏感信息
            data = settings.model_dump(mode="json", exclude={"config_sources"})
            # 移除 SecretStr 字段的值
            _redact_secrets(data)

        yaml_output = yaml.safe_dump(
            data, allow_unicode=True, default_flow_style=False, sort_keys=False
        )

        if output:
            Path(output).write_text(yaml_output, encoding="utf-8")
            click.echo(f"✓ 配置已导出到: {output}")
        else:
            click.echo(yaml_output)

        if not include_secrets:
            click.echo(
                "\n⚠️  敏感字段已隐藏，使用 --include-secrets 导出完整配置（谨慎使用）",
                err=True,
            )

    except Exception as error:
        click.echo(f"❌ 配置导出失败: {error}", err=True)
        sys.exit(1)


def _redact_secrets(data: dict):
    """递归隐藏配置中的敏感字段。"""
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


@config_group.command("schema")
@click.option("--output", "-o", type=click.Path(), help="输出文件路径")
def export_schema(output: str | None):
    """导出配置的 JSON Schema，用于 IDE 自动补全和验证。"""
    from app.core.config import Settings

    schema = Settings.model_json_schema()

    # 简化 schema，移除内部字段
    if "properties" in schema:
        schema["properties"].pop("config_sources", None)

    yaml_output = yaml.safe_dump(
        schema, allow_unicode=True, default_flow_style=False, sort_keys=False
    )

    if output:
        Path(output).write_text(yaml_output, encoding="utf-8")
        click.echo(f"✓ Schema 已导出到: {output}")
    else:
        click.echo(yaml_output)
