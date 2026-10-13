"""幂等性管理：基于客户端提供的幂等键，确保相同请求不会重复执行。"""

from __future__ import annotations

import json
from typing import Any

from redis.exceptions import RedisError

from app.core.logging import Throttle

# 幂等键 TTL：24 小时
IDEMPOTENCY_TTL = 86400

_warnings = Throttle(60)


class IdempotencyManager:
    """幂等性管理器；基于幂等键存储请求结果，防止重复执行。"""

    def __init__(self, redis, logger):
        self.redis = redis
        self.logger = logger

    def _key(self, tenant_id: str, idempotency_key: str) -> str:
        """生成 Redis 键：租户隔离的幂等键。"""
        return f"idempotency:{tenant_id}:{idempotency_key}"

    async def get(self, tenant_id: str, idempotency_key: str) -> dict | None:
        """获取已存在的请求结果；未找到返回 None。"""
        try:
            key = self._key(tenant_id, idempotency_key)
            cached = await self.redis.get(key)
            if cached:
                return json.loads(cached)
        except (RedisError, json.JSONDecodeError) as error:
            if _warnings.ready("idempotency_get"):
                self.logger.warning("幂等性检查失败", exception_type=type(error).__name__)
        return None

    async def set(
        self, tenant_id: str, idempotency_key: str, result: Any, ttl: int = IDEMPOTENCY_TTL
    ) -> None:
        """保存请求结果；用于幂等性保护。"""
        try:
            key = self._key(tenant_id, idempotency_key)
            payload = json.dumps(result)
            await self.redis.setex(key, ttl, payload)
        except (RedisError, TypeError) as error:
            # 幂等性保存失败不影响请求完成，但会记录警告
            if _warnings.ready("idempotency_set"):
                self.logger.warning("幂等性保存失败", exception_type=type(error).__name__)

    async def lock(self, tenant_id: str, idempotency_key: str, ttl: int = 300) -> bool:
        """尝试获取幂等锁；返回 True 表示获取成功，False 表示已有相同请求正在处理。"""
        try:
            key = self._key(tenant_id, idempotency_key) + ":lock"
            # NX：只在键不存在时设置
            acquired = await self.redis.set(key, "1", ex=ttl, nx=True)
            return acquired is not None
        except RedisError as error:
            # 锁获取失败时放行，避免阻断服务
            if _warnings.ready("idempotency_lock"):
                self.logger.warning("幂等锁获取失败", exception_type=type(error).__name__)
            return True

    async def unlock(self, tenant_id: str, idempotency_key: str) -> None:
        """释放幂等锁；请求完成后调用。"""
        try:
            key = self._key(tenant_id, idempotency_key) + ":lock"
            await self.redis.delete(key)
        except RedisError as error:
            # 锁释放失败不影响结果，依赖 TTL 自动过期
            if _warnings.ready("idempotency_unlock"):
                self.logger.warning("幂等锁释放失败", exception_type=type(error).__name__)
