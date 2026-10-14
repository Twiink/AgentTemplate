"""健康检查、详细诊断与运行指标采集；探针只执行低成本基础设施查询，不调用收费模型。"""

import asyncio
import time

from sqlalchemy import func, select, text

from app.core.logging import Throttle
from app.core.preflight import PreflightReport, run_preflight
from app.core.telemetry import active_runs, queue_depth
from app.db.models import Run

# 采集失败期间同类告警每分钟最多写一次。
_warnings = Throttle(60)


async def readiness(resources) -> dict:
    """就绪探针：只确认数据库和 Redis 可以连通；迁移版本等启动条件由启动预检负责。

    就绪状态决定流量是否进入本实例。迁移版本放在这里判断，会让先执行的迁移 Job
    使所有旧实例同时失去就绪，因此只保留连通性检查。
    """

    async def database():
        """执行最小查询确认数据库可用。"""
        async with resources.db.engine.connect() as connection:
            await connection.execute(text("SELECT 1"))

    async def redis():
        """用 PING 确认 Redis 可用。"""
        await resources.redis.ping()

    async def probe(check) -> dict:
        """限时执行单项检查，异常只返回类型名称，不输出连接参数。"""
        try:
            async with asyncio.timeout(3):
                await check()
            return {"status": "verified"}
        except Exception as error:
            return {"status": "failed", "exception_type": type(error).__name__}

    database_status, redis_status = await asyncio.gather(probe(database), probe(redis))
    components = {"database": database_status, "redis": redis_status}
    ready = all(item["status"] == "verified" for item in components.values())
    return {"ready": ready, "components": components}


class ReadinessCache:
    """把就绪结果缓存数秒；探针频繁调用时不放大数据库和 Redis 的压力。"""

    def __init__(self, ttl: float = 2.0):
        self.ttl = ttl
        self.value: dict | None = None
        self.checked = 0.0
        self.lock = asyncio.Lock()

    async def get(self, resources) -> dict:
        """返回缓存结果；过期时由一个请求重新检查，其余并发请求等待同一结果。"""
        async with self.lock:
            if self.value is None or time.monotonic() - self.checked >= self.ttl:
                self.value = await readiness(resources)
                self.checked = time.monotonic()
            return self.value


async def collect_run_metrics(resources) -> None:
    """统计排队和执行中的运行数量，写入进程内指标。"""
    async with resources.db.transaction(system=True) as session:
        rows = (
            await session.execute(
                select(Run.status, func.count())
                .where(Run.status.in_(["queued", "running"]))
                .group_by(Run.status)
            )
        ).all()
    counts = {status: count for status, count in rows}
    queue_depth.set(counts.get("queued", 0))
    active_runs.set(counts.get("running", 0))


async def run_metrics_collector(resources, interval: float = 15) -> None:
    """后台周期采集；/metrics 只读取内存中的值，数据库异常时保留上次数值并限频告警。"""
    while True:
        try:
            await collect_run_metrics(resources)
            resources.db.update_pool_metrics()
        except Exception as error:
            if _warnings.ready("collector"):
                resources.logger.warning(
                    "运行指标采集失败", exception_type=type(error).__name__, exc_info=error
                )
        await asyncio.sleep(interval)


async def diagnostics(container, report: PreflightReport | None = None) -> dict:
    """详细诊断：启动预检结果、Trace、Agent 与 MCP 状态，加上各启用模块的自检；需要认证后访问。"""
    report = report or await run_preflight(container.settings)
    results: dict = {
        "tracing": {
            "status": container.telemetry.tracing_status,
            "instrumentation_failures": list(container.telemetry.instrumentation_failures),
        },
        "sentry": {
            "status": container.telemetry.sentry_status,
        },
        "agents": {
            "status": "configured" if container.agents.describe() else "unconfigured",
            "count": len(container.agents.describe()),
        },
        "mcp": {},
    }
    for name in container.settings.mcp:
        try:
            await container.mcp.connect(name)
            results["mcp"][name] = {"status": "verified"}
        except Exception as error:
            results["mcp"][name] = {"status": "failed", "exception_type": type(error).__name__}
    for module in container.modules:
        if module.diagnose:
            results[module.info.name] = await module.diagnose(container)
    return {"ready": report.ok, "preflight": report.as_dict(), "components": results}
