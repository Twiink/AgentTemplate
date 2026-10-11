"""迁移入口；使用专门迁移连接，不在应用请求或 Worker 初始化中变更表结构。"""

import asyncio

from alembic import context
from sqlalchemy import pool
from sqlalchemy.ext.asyncio import create_async_engine

from app.core.config import load_settings
from app.db.models import Base

config = context.config
settings = load_settings()
url = (settings.migration_database_url or settings.database_url).get_secret_value()


def perform(connection):
    """绑定已打开的迁移连接，单事务执行版本链。"""
    context.configure(connection=connection, target_metadata=Base.metadata, compare_type=True)
    with context.begin_transaction():
        context.run_migrations()


async def online():
    """使用无池连接执行迁移，确保进程退出时释放数据库资源。"""
    engine = create_async_engine(url, poolclass=pool.NullPool)
    async with engine.connect() as connection:
        await connection.run_sync(perform)
    await engine.dispose()


if context.is_offline_mode():
    context.configure(url=url, target_metadata=Base.metadata, literal_binds=True)
    with context.begin_transaction():
        context.run_migrations()
else:
    asyncio.run(online())
