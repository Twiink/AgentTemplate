"""验证 API Key 和 JWT，并产生所有服务共享的认证主体。"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
from dataclasses import dataclass
from functools import cached_property

import jwt
from fastapi import Request

from app.core.config import AuthConfig
from app.core.errors import AppError


@dataclass(frozen=True)
class Principal:
    """可信主体；tenant_id 与 subject_id 只能来自凭据，scopes 限制资源与工具访问。"""

    tenant_id: str
    subject_id: str
    scopes: frozenset[str]

    def require(self, scope: str) -> None:
        """拒绝缺少所需权限的操作；抛出不包含凭据的 403 错误。"""
        if scope not in self.scopes:
            raise AppError("forbidden", "权限不足", 403)


class Authenticator:
    """保留 JWKS 缓存的验证器；远程取钥在线程执行，避免阻塞事件循环。"""

    def __init__(self, config: AuthConfig):
        self.config = config

    @cached_property
    def jwks(self):
        """惰性创建取钥客户端；只有 JWT 请求会连接 JWKS 服务。"""
        if self.config.jwks_url is None:
            raise RuntimeError("JWKS 未配置")
        return jwt.PyJWKClient(self.config.jwks_url, cache_keys=True, timeout=5)

    async def verify(self, token: str) -> Principal:
        """先验证 Key 摘要再验证 JWT；JWT 强制 issuer、audience、exp 与主体字段。"""
        digest = hashlib.sha256(token.encode()).hexdigest()
        for entry in self.config.api_keys:
            if hmac.compare_digest(digest, entry.digest):
                return Principal(entry.tenant_id, entry.subject_id, frozenset(entry.scopes))
        if not (self.config.jwt_key or self.config.jwks_url):
            raise AppError("unauthorized", "凭据无效或认证未配置", 401)
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
            raise AppError("unauthorized", "JWT 验证失败", 401) from error


async def current_principal(request: Request) -> Principal:
    """FastAPI 身份依赖；拒绝匿名访问，并按可信主体实施 Redis 限流。"""
    header = request.headers.get("authorization", "")
    if not header.startswith("Bearer "):
        raise AppError("unauthorized", "需要 Bearer 凭据", 401)
    principal = await request.app.state.authenticator.verify(header[7:])
    request.state.principal = principal
    resources = request.app.state.resources
    key = hashlib.sha256(f"{principal.tenant_id}:{principal.subject_id}".encode()).hexdigest()
    count = await resources.redis.eval(
        "local n=redis.call('INCR',KEYS[1]); if n==1 then redis.call('EXPIRE',KEYS[1],60) end; return n",
        1,
        "rate:" + key,
    )
    if count > resources.settings.rate_limit_per_minute:
        raise AppError("rate_limited", "请求过于频繁", 429, True)
    return principal
