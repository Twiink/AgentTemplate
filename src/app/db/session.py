"""身份事务工厂；使用 SET LOCAL 防止连接池复用时租户身份泄漏，并记录超过阈值的慢查询。"""

import time
from contextlib import asynccontextmanager

import structlog
from sqlalchemy import event, text
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker, create_async_engine

from app.core.config import Settings
from app.core.security import Principal
from app.core.telemetry import db_pool_connections


def watch_slow_queries(engine: AsyncEngine, threshold_ms: int) -> None:
    """记录执行时间超过阈值的 SQL；只记语句模板和耗时，参数值可能含业务数据，不写入日志。"""
    logger = structlog.get_logger("app.db")

    @event.listens_for(engine.sync_engine, "before_cursor_execute")
    def started(conn, cursor, statement, parameters, context, executemany):
        """在本次执行上下文上记下开始时间；执行上下文随语句结束释放，失败时不会累积。"""
        if context is not None:
            context.agent_query_started = time.perf_counter()

    @event.listens_for(engine.sync_engine, "after_cursor_execute")
    def finished(conn, cursor, statement, parameters, context, executemany):
        """计算耗时，超过阈值时写一条警告。"""
        began = getattr(context, "agent_query_started", None)
        if began is None:
            return
        elapsed = (time.perf_counter() - began) * 1000
        if elapsed >= threshold_ms:
            logger.warning(
                "慢查询", elapsed_ms=round(elapsed, 1), statement=" ".join(statement.split())[:1000]
            )


class Database:
    """持有异步连接池；system 事务只供内部调度，业务服务必须传入可信主体。"""

    def __init__(self, settings: Settings):
        self.engine = create_async_engine(
            settings.database_url.get_secret_value(),
            pool_size=settings.database_pool_size,
            max_overflow=0,
            pool_pre_ping=True,
        )
        if settings.slow_query_ms:
            watch_slow_queries(self.engine, settings.slow_query_ms)
        self.factory = async_sessionmaker(self.engine, expire_on_commit=False)

    @asynccontextmanager
    async def transaction(self, principal: Principal | None = None, *, system: bool = False):
        """创建提交或回滚的事务；退出后数据库自动清除本次身份变量。"""
        if principal is None and not system:
            raise ValueError("数据事务必须提供身份")
        async with self.factory() as session, session.begin():
            values = {
                "tenant": principal.tenant_id if principal else "",
                "subject": principal.subject_id if principal else "",
                "system": "true" if system else "false",
            }
            await session.execute(
                text(
                    "SELECT set_config('app.tenant_id', :tenant, true), "
                    "set_config('app.subject_id', :subject, true), set_config('app.system', :system, true)"
                ),
                values,
            )
            yield session

    async def close(self) -> None:
        """释放全部连接；调用者应在 MCP、运行任务等消费者停止后执行。"""
        await self.engine.dispose()

    def update_pool_metrics(self) -> None:
        """更新连接池指标；由周期采集调用。"""
        pool = self.engine.pool
        db_pool_connections.labels("in_use").set(pool.checkedout())
        db_pool_connections.labels("idle").set(pool.size() - pool.checkedout())
