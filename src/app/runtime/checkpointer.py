"""检查点连接：基于连接池的 AsyncPostgresSaver，与业务连接池分开，避免互相耗尽。

模板原先用单个连接承载全部检查点读写，并发运行会互相排队；这里改为独立连接池，
并单独提供线程互斥使用的 fence 连接，使 advisory lock 不再占用检查点池的连接。
"""

from __future__ import annotations

import asyncio
from typing import Any

from app.core.config import Settings


def dsn(settings: Settings) -> str:
    """把异步连接串转换为 psycopg 可用的同步驱动连接串。"""
    from sqlalchemy.engine import make_url

    url = make_url(settings.database_url.get_secret_value()).set(drivername="postgresql")
    return url.render_as_string(hide_password=False)


class FenceLock:
    """会话级 advisory lock 的独立连接池。

    fence 连接在整个运行期间保持占用，若与业务连接池共用，长时间运行会把池耗尽。
    这里使用只含少量连接的独立池，并允许等待取连接，避免并发运行直接失败。
    """

    def __init__(self, settings: Settings):
        self.settings = settings
        self.pool: Any = None
        self._lock = asyncio.Lock()

    async def _pool(self):
        """惰性建立独立池；池内连接数按允许并发运行数留出余量。"""
        async with self._lock:
            if self.pool is None:
                from psycopg_pool import AsyncConnectionPool

                self.pool = AsyncConnectionPool(
                    dsn(self.settings),
                    min_size=1,
                    max_size=max(2, self.settings.database_pool_size // 2),
                    open=False,
                    kwargs={"autocommit": True},
                )
                await self.pool.open()
            return self.pool

    @staticmethod
    def key(namespace: str) -> int:
        """把线程命名空间映射为有符号 64 位锁键；同一线程恒定得到同一键。"""
        import hashlib

        return int.from_bytes(hashlib.sha256(namespace.encode()).digest()[:8], "big", signed=True)

    async def hold(self, key: int):
        """尝试持有锁；拿不到时返回 None，调用者据此放弃本次执行。"""
        pool = await self._pool()
        connection = await pool.getconn()
        try:
            cursor = await connection.execute("SELECT pg_try_advisory_lock(%s)", (key,))
            acquired = (await cursor.fetchone())[0]
        except BaseException:
            await pool.putconn(connection)
            raise
        if not acquired:
            await pool.putconn(connection)
            return None
        return connection

    async def release(self, connection, key: int) -> None:
        """释放锁并归还连接；释放失败也必须归还，否则池会持续缩小。"""
        if self.pool is None:
            return
        try:
            await connection.execute("SELECT pg_advisory_unlock(%s)", (key,))
        finally:
            await self.pool.putconn(connection)

    async def close(self) -> None:
        """关闭独立池。"""
        if self.pool is not None:
            await self.pool.close()
            self.pool = None


class CheckpointPool:
    """检查点连接池的持有者；在资源容器生命周期内只建立一次。"""

    def __init__(self, settings: Settings):
        self.settings = settings
        self.saver: Any = None
        self.pool: Any = None
        self._lock = asyncio.Lock()

    async def saver_instance(self):
        """惰性建立池与 Saver；并发调用共用同一个实例。

        连接必须使用 dict_row：Saver 按列名读取检查点字段，元组行会导致取值失败。
        池的泛型参数显式声明 DictRow，使 Saver 的连接类型与实现一致。
        """
        async with self._lock:
            if self.saver is not None:
                return self.saver
            from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
            from psycopg import AsyncConnection
            from psycopg.rows import DictRow, dict_row
            from psycopg_pool import AsyncConnectionPool

            self.pool: AsyncConnectionPool[AsyncConnection[DictRow]] = AsyncConnectionPool(
                dsn(self.settings),
                min_size=1,
                max_size=self.settings.database_pool_size,
                open=False,
                kwargs={
                    "autocommit": True,
                    "prepare_threshold": 0,
                    "row_factory": dict_row,
                },
            )
            await self.pool.open()
            self.saver = AsyncPostgresSaver(conn=self.pool)
            # 表结构由迁移创建，这里不重复执行 setup，避免运行时改表。
            return self.saver

    async def close(self) -> None:
        """关闭连接池；Saver 本身不持有独立连接，无需单独关闭。"""
        if self.pool is not None:
            await self.pool.close()
            self.pool = None
            self.saver = None
