"""API Key 管理命令：创建、列举和吊销密钥。"""

import hashlib
import secrets
from datetime import UTC, datetime, timedelta

import click
from sqlalchemy import select

from app.cli.app import cli
from app.cli.common import async_command
from app.core.container import Container
from app.db.models import ApiKey


def generate_key() -> str:
    """生成 32 字节随机密钥，编码为 Base64URL 格式。"""
    return secrets.token_urlsafe(32)


def hash_key(key: str) -> str:
    """计算密钥的 SHA-256 摘要。"""
    return hashlib.sha256(key.encode()).hexdigest()


@cli.group("api-key")
def api_key_group():
    """API Key 管理：创建、列举和吊销密钥。"""
    pass


@api_key_group.command("create")
@click.option("--name", required=True, help="密钥名称")
@click.option("--tenant-id", required=True, help="租户 ID")
@click.option("--subject-id", required=True, help="主体 ID")
@click.option(
    "--scope",
    "scopes",
    multiple=True,
    default=["resources:read", "resources:write"],
    help="权限作用域，可多次指定",
)
@click.option("--expires-days", type=int, help="过期天数，不指定则永不过期")
@async_command
async def create_key(
    name: str, tenant_id: str, subject_id: str, scopes: tuple, expires_days: int | None
):
    """创建新的 API Key。"""
    key = generate_key()
    digest = hash_key(key)
    expires_at = None
    if expires_days:
        expires_at = datetime.now(UTC) + timedelta(days=expires_days)

    async with Container.startup() as container:
        async with container.db.transaction(system=True) as session:
            api_key = ApiKey(
                tenant_id=tenant_id,
                subject_id=subject_id,
                name=name,
                digest=digest,
                scopes=list(scopes),
                revoked=False,
                expires_at=expires_at,
            )
            session.add(api_key)

        click.echo("\n创建成功")
        click.echo(f"密钥 ID: {api_key.id}")
        click.echo(f"名称: {name}")
        click.echo(f"租户: {tenant_id}")
        click.echo(f"主体: {subject_id}")
        click.echo(f"作用域: {', '.join(scopes)}")
        if expires_at:
            click.echo(f"过期时间: {expires_at.isoformat()}")
        click.echo("\n请保存以下密钥，它只显示一次：")
        click.echo(f"\n{key}\n")


@api_key_group.command("list")
@click.option("--tenant-id", help="按租户 ID 过滤")
@click.option("--subject-id", help="按主体 ID 过滤")
@click.option("--include-revoked", is_flag=True, help="包含已吊销的密钥")
@async_command
async def list_keys(tenant_id: str | None, subject_id: str | None, include_revoked: bool):
    """列举 API Keys。"""
    async with Container.startup() as container:
        async with container.db.transaction(system=True) as session:
            query = select(ApiKey)
            if tenant_id:
                query = query.where(ApiKey.tenant_id == tenant_id)
            if subject_id:
                query = query.where(ApiKey.subject_id == subject_id)
            if not include_revoked:
                query = query.where(ApiKey.revoked.is_(False))

            query = query.order_by(ApiKey.created_at.desc())
            keys = (await session.scalars(query)).all()

        if not keys:
            click.echo("未找到 API Keys")
            return

        click.echo(f"\n共找到 {len(keys)} 个密钥：\n")
        for key in keys:
            status = "已吊销" if key.revoked else "有效"
            if key.expires_at and key.expires_at < datetime.now(UTC):
                status = "已过期"

            click.echo(f"ID: {key.id}")
            click.echo(f"  名称: {key.name}")
            click.echo(f"  租户: {key.tenant_id}")
            click.echo(f"  主体: {key.subject_id}")
            click.echo(f"  作用域: {', '.join(key.scopes)}")
            click.echo(f"  状态: {status}")
            click.echo(f"  创建时间: {key.created_at.isoformat()}")
            if key.last_used_at:
                click.echo(f"  最近使用: {key.last_used_at.isoformat()}")
            if key.expires_at:
                click.echo(f"  过期时间: {key.expires_at.isoformat()}")
            click.echo()


@api_key_group.command("revoke")
@click.argument("key_id")
@async_command
async def revoke_key(key_id: str):
    """吊销指定的 API Key。"""
    async with Container.startup() as container:
        async with container.db.transaction(system=True) as session:
            key = await session.get(ApiKey, key_id)
            if not key:
                click.echo(f"未找到密钥: {key_id}", err=True)
                raise SystemExit(1)

            if key.revoked:
                click.echo(f"密钥已经被吊销: {key.name}")
                return

            key.revoked = True

        click.echo(f"已吊销密钥: {key.name} ({key_id})")
