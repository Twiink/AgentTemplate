"""响应缓存：为相同输入的运行返回缓存结果，减少重复 LLM 调用。"""

from __future__ import annotations

import hashlib
import json
from typing import Any

from redis.exceptions import RedisError

from app.core.logging import Throttle

# 缓存键 TTL：24 小时
CACHE_TTL = 86400

_warnings = Throttle(60)


class ResponseCache:
    """运行响应缓存；基于输入哈希，Redis 故障时降级为不缓存。"""

    def __init__(self, redis, logger):
        self.redis = redis
        self.logger = logger

    def _key(self, agent_id: str, input_data: dict, options: dict) -> str:
        """生成缓存键：agent_id + 输入 + 选项的哈希。"""
        payload = json.dumps(
            {"agent_id": agent_id, "input": input_data, "options": options},
            sort_keys=True,
        )
        digest = hashlib.sha256(payload.encode()).hexdigest()
        return f"cache:run:{digest}"

    async def get(self, agent_id: str, input_data: dict, options: dict) -> dict | None:
        """获取缓存响应；未命中或 Redis 故障返回 None。"""
        try:
            key = self._key(agent_id, input_data, options)
            cached = await self.redis.get(key)
            if cached:
                return json.loads(cached)
        except (RedisError, json.JSONDecodeError) as error:
            if _warnings.ready("cache_get"):
                self.logger.warning("缓存读取失败", exception_type=type(error).__name__)
        return None

    async def set(self, agent_id: str, input_data: dict, options: dict, output: Any) -> None:
        """保存运行输出到缓存；只缓存成功的结果。"""
        try:
            key = self._key(agent_id, input_data, options)
            payload = json.dumps(output)
            await self.redis.setex(key, CACHE_TTL, payload)
        except (RedisError, TypeError) as error:
            # 缓存写入失败不影响运行完成
            if _warnings.ready("cache_set"):
                self.logger.warning("缓存写入失败", exception_type=type(error).__name__)

    async def invalidate(self, agent_id: str) -> None:
        """使某个 Agent 的所有缓存失效；用于 Agent 更新后。"""
        try:
            pattern = "cache:run:*"
            cursor = 0
            while True:
                cursor, keys = await self.redis.scan(cursor, match=pattern, count=100)
                if keys:
                    await self.redis.delete(*keys)
                if cursor == 0:
                    break
        except RedisError as error:
            if _warnings.ready("cache_invalidate"):
                self.logger.warning("缓存失效失败", exception_type=type(error).__name__)
