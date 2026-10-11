"""API、CLI 和 Worker 共用的资源容器；按逆序关闭依赖，导入模块不连接外部服务。"""

from __future__ import annotations

import importlib
import inspect
from contextlib import AsyncExitStack

import httpx
from redis.asyncio import Redis

from app.core.config import Settings
from app.core.observability import Telemetry, setup_logging
from app.db.session import Database
from app.runtime.contracts import AgentRegistry
from app.services.models import ModelRegistry
from app.services.prompts import PromptLoader
from app.services.storage import LocalStore, S3Store


class Resources:
    """统一生命周期；原生 Checkpointer 惰性连接，仅执行对应框架时需要。"""

    def __init__(self, settings: Settings):
        self.settings = settings
        self.stack = AsyncExitStack()
        self.logger = setup_logging(settings)
        self.telemetry = Telemetry(settings)
        self.agents = AgentRegistry()
        self.checkpointer = None
        self._checkpoint_lock = None

    async def _open(self):
        """建立共享客户端及注册项；未配置模型不影响基础服务启动。"""
        import asyncio

        self._checkpoint_lock = asyncio.Lock()
        self.stack.callback(self.telemetry.close)
        self.db = Database(self.settings)
        self.stack.push_async_callback(self.db.close)
        self.redis = Redis.from_url(
            self.settings.redis_url.get_secret_value(),
            decode_responses=True,
            socket_connect_timeout=5,
            socket_timeout=5,
        )
        self.stack.push_async_callback(self.redis.aclose)
        proxies = {
            p.proxy or ""
            for p in [*self.settings.chat_models.values(), *self.settings.embedding_models.values()]
        } | {""}
        self.http = {}
        for proxy in proxies:
            self.http[proxy] = await self.stack.enter_async_context(
                httpx.AsyncClient(
                    proxy=proxy or None, timeout=30, follow_redirects=False, trust_env=False
                )
            )
        self.models = ModelRegistry(self.settings, self.http, self.telemetry)
        self.stack.push_async_callback(self.models.close)
        self.prompts = PromptLoader(self.settings.prompts_directory)
        self.storage = (
            LocalStore(self.settings.storage.directory)
            if self.settings.storage.backend == "local"
            else S3Store(self.settings.storage)
        )
        if isinstance(self.storage, S3Store):
            self.stack.push_async_callback(self.storage.close)
        from app.services.tools import ToolRegistry

        self.tools = ToolRegistry(self)
        from app.services.mcp import MCPManager

        self.mcp = MCPManager(self)
        self.stack.push_async_callback(self.mcp.close)
        from app.services.data import DataService
        from app.services.knowledge import KnowledgeService
        from app.services.runs import RunService

        self.data = DataService(self)
        self.knowledge = KnowledgeService(self)
        self.runs = RunService(self)
        from app.services.extraction import ExtractionService

        self.extraction = ExtractionService(self)
        for reference in self.settings.registrations:
            module, symbol = reference.split(":", 1)
            register = getattr(importlib.import_module(module), symbol)
            result = register(self)
            if inspect.isawaitable(result):
                await result
        return self

    async def __aenter__(self):
        """初始化失败也释放部分成功资源；调用者不必额外模拟退出流程。"""
        try:
            return await self._open()
        except BaseException:
            await self.stack.aclose()
            raise

    async def checkpoint(self):
        """进入原生 Saver 上下文；共享连接有独立锁，迁移只在 CLI 中执行。"""
        if self._checkpoint_lock is None:
            raise RuntimeError("资源容器尚未进入 lifespan")
        async with self._checkpoint_lock:
            if self.checkpointer is None:
                from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
                from sqlalchemy.engine import make_url

                url = make_url(self.settings.database_url.get_secret_value()).set(
                    drivername="postgresql"
                )
                self.checkpointer = await self.stack.enter_async_context(
                    AsyncPostgresSaver.from_conn_string(url.render_as_string(hide_password=False))
                )
            return self.checkpointer

    async def __aexit__(self, *args):
        """即使某一初始化步骤失败，也释放此前已获得的全部资源。"""
        await self.stack.aclose()
