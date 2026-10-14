"""API Key 管理命令：创建、列举和吊销密钥。"""

from __future__ import annotations

import asyncio
import hashlib
import secrets
from datetime import UTC, datetime, timedelta
from typing import Annotated

import typer
from sqlalchemy import select

from app.cli.app import cli
from app.cli.common import ensure_ready, settings_or_exit
from app.core.container import Container
from app.db.models import ApiKey

api_key_cli = typer.Typer(help="API Key 管理：创建、列举和吊销密钥。", no_args_is_help=True)
cli.add_typer(api_key_cli, name="api-key")


def generate_key() -> str:
    """生成 32 字节随机密钥，编码为 Base64URL 格式。"""
    return secrets.token_urlsafe(32)


def hash_key(key: str) -> str:
    """计算 API Key 的 SHA-256 摘要。"""
    return hashlib.sha256(key.encode()).hexdigest()


@api_key_cli.command("create")
def create_key(
    name: Annotated[str, typer.Option(..., "--name", help="密钥名称")],
    tenant_id: Annotated[str, typer.Option(..., "--tenant-id", help="租户 ID")],
    subject_id: Annotated[str, typer.Option(..., "--subject-id", help="主体 ID")],
    scopes: Annotated[
        list[str] | None,
        typer.Option("--scope", help="权限作用域，可多次指定；默认包含读写资源权限"),
    ] = None,
    expires_days: Annotated[
        int | None, typer.Option("--expires-days", help="过期天数，不指定则永不过期")
    ] = None,
) -> None:
    """创建新的 API Key。"""
    settings = settings_or_exit()
    ensure_ready(settings)

    async def run() -> None:
        """创建凭据记录，并在数据库提交后一次性显示原始密钥。"""
        key = generate_key()
        digest = hash_key(key)
        selected_scopes = scopes or ["resources:read", "resources:write"]
        expires_at = datetime.now(UTC) + timedelta(days=expires_days) if expires_days else None

        async with Container(settings) as container:
            async with container.db.transaction(system=True) as session:
                api_key = ApiKey(
                    tenant_id=tenant_id,
                    subject_id=subject_id,
                    name=name,
                    digest=digest,
                    scopes=selected_scopes,
                    revoked=False,
                    expires_at=expires_at,
                )
                session.add(api_key)

        typer.echo("\n创建成功")
        typer.echo(f"密钥 ID: {api_key.id}")
        typer.echo(f"名称: {name}")
        typer.echo(f"租户: {tenant_id}")
        typer.echo(f"主体: {subject_id}")
        typer.echo(f"作用域: {', '.join(selected_scopes)}")
        if expires_at:
            typer.echo(f"过期时间: {expires_at.isoformat()}")
        typer.echo("\n请保存以下密钥，它只显示一次：")
        typer.echo(f"\n{key}\n")

    asyncio.run(run())


@api_key_cli.command("list")
def list_keys(
    tenant_id: Annotated[str | None, typer.Option("--tenant-id", help="按租户 ID 过滤")] = None,
    subject_id: Annotated[str | None, typer.Option("--subject-id", help="按主体 ID 过滤")] = None,
    include_revoked: Annotated[
        bool, typer.Option("--include-revoked", help="包含已吊销的密钥")
    ] = False,
) -> None:
    """列举 API Keys。"""
    settings = settings_or_exit()
    ensure_ready(settings)

    async def run() -> list[ApiKey]:
        """按筛选条件读取数据库中的密钥记录。"""
        async with Container(settings) as container:
            async with container.db.transaction(system=True) as session:
                query = select(ApiKey)
                if tenant_id:
                    query = query.where(ApiKey.tenant_id == tenant_id)
                if subject_id:
                    query = query.where(ApiKey.subject_id == subject_id)
                if not include_revoked:
                    query = query.where(ApiKey.revoked.is_(False))

                query = query.order_by(ApiKey.created_at.desc())
                return list((await session.scalars(query)).all())

    keys = asyncio.run(run())
    if not keys:
        typer.echo("未找到 API Keys")
        return

    typer.echo(f"\n共找到 {len(keys)} 个密钥：\n")
    for api_key in keys:
        status = "已吊销" if api_key.revoked else "有效"
        if api_key.expires_at and api_key.expires_at < datetime.now(UTC):
            status = "已过期"

        typer.echo(f"ID: {api_key.id}")
        typer.echo(f"  名称: {api_key.name}")
        typer.echo(f"  租户: {api_key.tenant_id}")
        typer.echo(f"  主体: {api_key.subject_id}")
        typer.echo(f"  作用域: {', '.join(api_key.scopes)}")
        typer.echo(f"  状态: {status}")
        typer.echo(f"  创建时间: {api_key.created_at.isoformat()}")
        if api_key.last_used_at:
            typer.echo(f"  最近使用: {api_key.last_used_at.isoformat()}")
        if api_key.expires_at:
            typer.echo(f"  过期时间: {api_key.expires_at.isoformat()}")
        typer.echo()


@api_key_cli.command("revoke")
def revoke_key(
    key_id: Annotated[str, typer.Argument(..., help="需要吊销的密钥 ID")],
) -> None:
    """吊销指定的 API Key。"""
    settings = settings_or_exit()
    ensure_ready(settings)

    async def run() -> tuple[str, bool, bool]:
        """锁定指定密钥并返回名称、是否存在及是否已吊销。"""
        async with Container(settings) as container:
            async with container.db.transaction(system=True) as session:
                api_key = await session.get(ApiKey, key_id)
                if api_key is None:
                    return "", False, False
                if api_key.revoked:
                    return api_key.name, True, True
                api_key.revoked = True
                return api_key.name, True, False

    name, found, already_revoked = asyncio.run(run())
    if not found:
        typer.echo(f"未找到密钥: {key_id}", err=True)
        raise typer.Exit(1)
    if already_revoked:
        typer.echo(f"密钥已经被吊销: {name}")
        return
    typer.echo(f"已吊销密钥: {name} ({key_id})")
