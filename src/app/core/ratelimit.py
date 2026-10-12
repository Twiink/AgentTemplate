"""资源接口限流：按可信主体做每分钟固定窗口计数；Redis 故障时按配置放行或拒绝，并记录指标。"""

from __future__ import annotations

import hashlib
from typing import Annotated

from fastapi import Depends, Request
from redis.exceptions import RedisError

from app.core.errors import AppError
from app.core.logging import Throttle
from app.core.security import Principal, current_principal
from app.core.telemetry import ratelimit_backend_errors, ratelimit_rejections

WINDOW_SCRIPT = (
    "local n=redis.call('INCR',KEYS[1]); if n==1 then redis.call('EXPIRE',KEYS[1],60) end; return n"
)

# 后端故障期间同类告警每分钟最多写一次，避免每个请求都写日志。
_warnings = Throttle(60)


async def limited_principal(
    request: Request, principal: Annotated[Principal, Depends(current_principal)]
) -> Principal:
    """资源接口的身份依赖：先认证再计数，超过每分钟上限返回 429。"""
    resources = request.app.state.container
    settings = resources.settings
    route = getattr(request.scope.get("route"), "path", "unmatched")
    identity = f"{principal.tenant_id}:{principal.subject_id}"
    key = "rate:" + hashlib.sha256(identity.encode()).hexdigest()
    try:
        count = await resources.redis.eval(WINDOW_SCRIPT, 1, key)
    except (RedisError, OSError) as error:
        # 限流是保护措施而不是业务依赖；故障时按部署方选择的策略处理，不能变成未分类的 500。
        ratelimit_backend_errors.labels(route).inc()
        if _warnings.ready("backend"):
            resources.logger.warning(
                "限流后端不可用",
                failure_mode=settings.rate_limit_failure_mode,
                exception_type=type(error).__name__,
            )
        if settings.rate_limit_failure_mode == "closed":
            raise AppError("rate_limit_unavailable", "限流服务暂不可用", 503, True) from error
        return principal
    if count > settings.rate_limit_per_minute:
        ratelimit_rejections.labels(route).inc()
        raise AppError("rate_limited", "请求过于频繁", 429, True)
    return principal
