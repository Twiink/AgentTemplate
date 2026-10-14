"""费用配额管理：按租户/主体跟踪 LLM 用量，超过配额拒绝新运行。"""

from __future__ import annotations

import hashlib
from datetime import UTC, datetime

from redis.exceptions import RedisError

from app.core.errors import AppError, Code
from app.core.logging import Throttle
from app.core.security import Principal

# 配额哈希以 run ID 记账，终态回调重试不会重复扣费。
QUOTA_SCRIPT = """
local current = tonumber(redis.call('HGET', KEYS[1], 'total') or '0')
if redis.call('HEXISTS', KEYS[1], ARGV[2]) == 1 then return current end
local new_value = current + tonumber(ARGV[1])
redis.call('HSET', KEYS[1], 'total', new_value, ARGV[2], '1')
redis.call('EXPIRE', KEYS[1], 2592000)
return new_value
"""

_warnings = Throttle(60)


class QuotaManager:
    """费用配额管理器；Redis 故障时放行，不阻断服务。"""

    def __init__(self, redis, logger, settings):
        self.redis = redis
        self.logger = logger
        self.settings = settings

    def _key(self, principal: Principal) -> str:
        """生成版本化主体配额哈希键。"""
        identity = f"{principal.tenant_id}:{principal.subject_id}"
        digest = hashlib.sha256(identity.encode()).hexdigest()
        month = datetime.now(UTC).strftime("%Y%m")
        return f"quota:v2:{digest}:{month}"

    def _legacy_key(self, principal: Principal) -> str:
        """返回旧版字符串配额键，用于升级时保留已经累计的月度用量。"""
        identity = f"{principal.tenant_id}:{principal.subject_id}"
        digest = hashlib.sha256(identity.encode()).hexdigest()
        month = datetime.now(UTC).strftime("%Y%m")
        return f"quota:{digest}:{month}"

    async def _migrate_legacy_total(self, principal: Principal, key: str):
        """首次访问时把旧字符串累计值复制到新版幂等配额哈希。"""
        current = await self.redis.hget(key, "total")
        if current is not None:
            return current
        legacy = await self.redis.get(self._legacy_key(principal))
        if legacy is None:
            return None
        await self.redis.hsetnx(key, "total", legacy)
        await self.redis.expire(key, 2_592_000)
        return await self.redis.hget(key, "total")

    async def check(self, principal: Principal) -> None:
        """检查配额是否可用；超出时拒绝新运行。"""
        if not self.settings.monthly_cost_budget:
            return

        try:
            key = self._key(principal)
            current = await self._migrate_legacy_total(principal, key)
            used = float(current) if current else 0.0

            if used >= self.settings.monthly_cost_budget:
                raise AppError(
                    Code.QUOTA_EXCEEDED,
                    f"本月费用配额已用尽 (${used:.4f}/${self.settings.monthly_cost_budget:.2f})",
                    429,
                    True,
                )
        except RedisError as error:
            # 配额后端故障时放行，避免阻断服务
            if _warnings.ready("quota_backend"):
                self.logger.warning("配额后端不可用", exception_type=type(error).__name__)

    async def record(self, principal: Principal, cost: float, *, run_id: str) -> None:
        """按运行 ID 幂等记录费用，避免终态提交重试重复计费。"""
        if not self.settings.monthly_cost_budget or cost <= 0:
            return

        try:
            key = self._key(principal)
            await self._migrate_legacy_total(principal, key)
            await self.redis.eval(QUOTA_SCRIPT, 1, key, cost, f"run:{run_id}")
        except RedisError as error:
            # 记录失败不影响运行完成
            if _warnings.ready("quota_record"):
                self.logger.warning("配额记录失败", exception_type=type(error).__name__)

    async def usage(self, principal: Principal) -> dict:
        """查询当前配额使用情况。"""
        if not self.settings.monthly_cost_budget:
            return {"enabled": False}

        try:
            key = self._key(principal)
            current = await self._migrate_legacy_total(principal, key)
            used = float(current) if current else 0.0

            return {
                "enabled": True,
                "used": used,
                "limit": self.settings.monthly_cost_budget,
                "remaining": max(0, self.settings.monthly_cost_budget - used),
                "month": datetime.now(UTC).strftime("%Y-%m"),
            }
        except RedisError:
            return {"enabled": True, "error": "配额后端不可用"}
