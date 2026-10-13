"""验证 API Key 和 JWT，并产生所有服务共享的认证主体；限流在 app.core.ratelimit 中独立执行。"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
from dataclasses import dataclass
from datetime import UTC, datetime
from functools import cached_property

import jwt
import structlog
from fastapi import Request
from sqlalchemy import select

from app.core.config import AuthConfig
from app.core.errors import AppError, Code


@dataclass(frozen=True)
class Principal:
    """可信主体；tenant_id 与 subject_id 只能来自凭据，scopes 限制资源与工具访问。"""

    tenant_id: str
    subject_id: str
    scopes: frozenset[str]
    # API Key 摘要前 8 位，只用于日志关联调用方，不能反推出原始凭据；JWT 身份为 None。
    key_prefix: str | None = None

    def require(self, scope: str) -> None:
        """拒绝缺少所需权限的操作；抛出不包含凭据的 403 错误。"""
        if scope not in self.scopes:
            raise AppError(Code.FORBIDDEN, "权限不足", 403)


class Authenticator:
    """保留 JWKS 缓存的验证器；远程取钥在线程执行，避免阻塞事件循环。"""

    def __init__(self, config: AuthConfig, db=None):
        self.config = config
        self.db = db

    @cached_property
    def jwks(self):
        """惰性创建取钥客户端；只有 JWT 请求会连接 JWKS 服务。"""
        if self.config.jwks_url is None:
            raise RuntimeError("JWKS 未配置")
        return jwt.PyJWKClient(self.config.jwks_url, cache_keys=True, timeout=5)

    async def verify(self, token: str) -> Principal:
        """先验证 Key 摘要再验证 JWT；JWT 强制 issuer、audience、exp 与主体字段。"""
        digest = hashlib.sha256(token.encode()).hexdigest()

        # 优先从配置文件验证静态 API Key
        for entry in self.config.api_keys:
            if hmac.compare_digest(digest, entry.digest):
                return Principal(
                    entry.tenant_id, entry.subject_id, frozenset(entry.scopes), entry.digest[:8]
                )

        # 从数据库验证动态 API Key
        if self.db:
            try:
                from app.db.models import ApiKey

                async with self.db.transaction(system=True) as session:
                    key = (
                        await session.scalars(
                            select(ApiKey).where(
                                ApiKey.digest == digest,
                                ApiKey.revoked.is_(False),
                            )
                        )
                    ).one_or_none()

                    if key:
                        # 检查是否过期
                        if key.expires_at and key.expires_at < datetime.now(UTC):
                            raise AppError(Code.UNAUTHORIZED, "API Key 已过期", 401)

                        # 更新最近使用时间（异步，不阻塞认证）
                        asyncio.create_task(self._update_last_used(key.id))

                        return Principal(
                            key.tenant_id, key.subject_id, frozenset(key.scopes), key.digest[:8]
                        )
            except Exception:
                pass

        if not (self.config.jwt_key or self.config.jwks_url):
            raise AppError(Code.UNAUTHORIZED, "凭据无效或认证未配置", 401)
        try:
            if self.config.jwks_url:
                key = (await asyncio.to_thread(self.jwks.get_signing_key_from_jwt, token)).key
            elif self.config.jwt_key:
                key = self.config.jwt_key.get_secret_value()
            else:
                raise ValueError("JWT 密钥未配置")
            claims = jwt.decode(
                token,
                key,
                algorithms=self.config.jwt_algorithms,
                issuer=self.config.jwt_issuer,
                audience=self.config.jwt_audience,
                options={"require": ["exp", "iss", "aud", "sub", "tenant_id"]},
            )
            if (
                not isinstance(claims["tenant_id"], str)
                or not isinstance(claims["sub"], str)
                or not claims["tenant_id"]
                or not claims["sub"]
            ):
                raise ValueError("身份格式无效")
            scope = claims.get("scope", "")
            if not isinstance(scope, str):
                raise ValueError("权限格式无效")
            return Principal(claims["tenant_id"], claims["sub"], frozenset(scope.split()))
        except (jwt.PyJWTError, ValueError, OSError) as error:
            raise AppError(Code.UNAUTHORIZED, "JWT 验证失败", 401) from error

    async def _update_last_used(self, key_id: str) -> None:
        """异步更新 API Key 最近使用时间；失败不影响认证。"""
        if not self.db:
            return
        try:
            from app.db.models import ApiKey

            async with self.db.transaction(system=True) as session:
                key = await session.get(ApiKey, key_id)
                if key:
                    key.last_used_at = datetime.now(UTC)
        except Exception:
            pass


async def current_principal(request: Request) -> Principal:
    """FastAPI 身份依赖；拒绝匿名访问，并把可信身份写入日志上下文。"""
    header = request.headers.get("authorization", "")
    if not header.startswith("Bearer "):
        raise AppError(Code.UNAUTHORIZED, "需要 Bearer 凭据", 401)
    principal = await request.app.state.authenticator.verify(header[7:])
    request.state.principal = principal
    structlog.contextvars.bind_contextvars(
        tenant_id=principal.tenant_id,
        subject_id=principal.subject_id,
        key_prefix=principal.key_prefix,
    )
    return principal
