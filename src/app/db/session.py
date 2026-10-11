"""身份事务工厂；使用 SET LOCAL 防止连接池复用时租户身份泄漏。"""

from contextlib import asynccontextmanager

from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.core.config import Settings
from app.core.security import Principal


class Database:
    """持有异步连接池；system 事务只供内部调度，业务服务必须传入可信主体。"""

    def __init__(self, settings: Settings):
        self.engine = create_async_engine(
            settings.database_url.get_secret_value(),
            pool_size=settings.database_pool_size,
            max_overflow=0,
            pool_pre_ping=True,
        )
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
