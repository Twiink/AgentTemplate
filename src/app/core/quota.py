"""费用配额管理：按租户/主体跟踪 LLM 用量，超过配额拒绝新运行。"""

from __future__ import annotations

import hashlib
from datetime import UTC, datetime

from redis.exceptions import RedisError

from app.core.errors import AppError, Code
from app.core.logging import Throttle
from app.core.security import Principal

# 配额脚本：原子递增并返回当前值，首次设置30天过期
QUOTA_SCRIPT = """
local current = redis.call('GET', KEYS[1])
if not current then current = 0 else current = tonumber(current) end
local new_value = current + tonumber(ARGV[1])
redis.call('SET', KEYS[1], new_value)
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
        """生成配额键：按租户或主体计量。"""
        identity = f"{principal.tenant_id}:{principal.subject_id}"
        digest = hashlib.sha256(identity.encode()).hexdigest()
        month = datetime.now(UTC).strftime("%Y%m")
        return f"quota:{digest}:{month}"

    async def check(self, principal: Principal) -> None:
        """检查配额是否可用；超出时拒绝新运行。"""
        if not self.settings.run_cost_budget:
            return

        try:
            key = self._key(principal)
            current = await self.redis.get(key)
            used = float(current) if current else 0.0

            if used >= self.settings.run_cost_budget:
                raise AppError(
                    Code.QUOTA_EXCEEDED,
                    f"本月费用配额已用尽 (${used:.4f}/${self.settings.run_cost_budget:.2f})",
                    429,
                    True,
                )
        except RedisError as error:
            # 配额后端故障时放行，避免阻断服务
            if _warnings.ready("quota_backend"):
                self.logger.warning("配额后端不可用", exception_type=type(error).__name__)

    async def record(self, principal: Principal, cost: float) -> None:
        """记录运行费用；用于运行完成后更新配额。"""
        if not self.settings.run_cost_budget or cost <= 0:
            return

        try:
            key = self._key(principal)
            await self.redis.eval(QUOTA_SCRIPT, 1, key, cost)
        except RedisError as error:
            # 记录失败不影响运行完成
            if _warnings.ready("quota_record"):
                self.logger.warning("配额记录失败", exception_type=type(error).__name__)

    async def usage(self, principal: Principal) -> dict:
        """查询当前配额使用情况。"""
        if not self.settings.run_cost_budget:
            return {"enabled": False}

        try:
            key = self._key(principal)
            current = await self.redis.get(key)
            used = float(current) if current else 0.0

            return {
                "enabled": True,
                "used": used,
                "limit": self.settings.run_cost_budget,
                "remaining": max(0, self.settings.run_cost_budget - used),
                "month": datetime.now(UTC).strftime("%Y-%m"),
            }
        except RedisError:
            return {"enabled": True, "error": "配额后端不可用"}
