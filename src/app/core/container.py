"""API、CLI 和 Worker 共用的资源容器：先建立核心资源，再按依赖顺序装配启用的模块，退出时逆序释放。

导入本模块不会连接外部服务；连接在进入 async with 时建立。
"""

from __future__ import annotations

import asyncio
import importlib
import inspect
from contextlib import AsyncExitStack
from typing import TYPE_CHECKING, Any

import httpx
from redis.asyncio import Redis

from app.core.config import Settings
from app.core.logging import setup_logging
from app.core.telemetry import Telemetry
from app.db.repository import Repository
from app.db.session import Database
from app.llm.gateway import ModelGateway
from app.modules import Module, load, resolve
from app.prompts.loader import PromptLoader
from app.runtime.contracts import AgentRegistry

if TYPE_CHECKING:
    from app.modules.background.service import BackgroundService
    from app.modules.knowledge.service import KnowledgeService
    from app.modules.memory.extraction import ExtractionService
    from app.modules.memory.service import MemoryService
    from app.modules.storage.service import ArtifactService
    from app.modules.storage.store import ArtifactStore
    from app.services.runs import RunService
    from app.services.threads import ThreadService
    from app.tools.mcp import MCPManager
    from app.tools.registry import ToolRegistry


class Container:
    """统一生命周期的资源容器。

    核心资源（数据库、Redis、模型、Prompt、工具、MCP、线程与运行服务）总是存在；
    模块服务只在对应模块启用后创建，未启用时为 None。原生 Checkpointer 惰性连接。
    """

    db: Database
    repository: Repository
    redis: Redis
    http: dict[str, httpx.AsyncClient]
    models: ModelGateway
    prompts: PromptLoader
    tools: ToolRegistry
    mcp: MCPManager
    threads: ThreadService
    runs: RunService

    def __init__(self, settings: Settings):
        self.settings = settings
        self.stack = AsyncExitStack()
        self.logger = setup_logging(settings)
        self.telemetry = Telemetry(settings)
        self.agents = AgentRegistry()
        self.modules: list[Module] = []
        self.checkpointer: Any = None
        self._checkpoint_lock: asyncio.Lock | None = None
        # 模块服务；对应模块未启用时保持 None。
        self.storage: ArtifactStore | None = None
        self.artifacts: ArtifactService | None = None
        self.background: BackgroundService | None = None
        self.knowledge: KnowledgeService | None = None
        self.memories: MemoryService | None = None
        self.extraction: ExtractionService | None = None

    def enabled(self, name: str) -> bool:
        """模块是否已启用并完成装配。"""
        return any(module.info.name == name for module in self.modules)

    async def _open(self):
        """建立核心资源，装配启用的模块，最后执行项目注册；未配置模型不影响基础服务启动。"""
        self._checkpoint_lock = asyncio.Lock()
        self.stack.callback(self.telemetry.close)
        self.db = Database(self.settings)
        self.stack.push_async_callback(self.db.close)
        self.repository = Repository(self.db)
        self.redis = Redis.from_url(
            self.settings.redis_url.get_secret_value(),
            decode_responses=True,
            socket_connect_timeout=5,
            socket_timeout=5,
        )
        self.stack.push_async_callback(self.redis.aclose)
        proxies = {
            p.proxy or ""
            for p in [*self.settings.llm.models.values(), *self.settings.llm.embeddings.values()]
        } | {""}
        self.http = {}
        for proxy in proxies:
            self.http[proxy] = await self.stack.enter_async_context(
                httpx.AsyncClient(
                    proxy=proxy or None, timeout=30, follow_redirects=False, trust_env=False
                )
            )
        self.models = ModelGateway(self.settings, self.http, self.telemetry)
        self.stack.push_async_callback(self.models.close)
        self.prompts = PromptLoader(self.settings.prompts_directory)
        # 服务按需导入：它们在类型标注中引用容器，延迟导入避免模块之间的循环依赖。
        from app.services.runs import RunService
        from app.services.threads import ThreadService
        from app.tools.mcp import MCPManager
        from app.tools.registry import ToolRegistry

        self.tools = ToolRegistry(self)
        self.mcp = MCPManager(self)
        self.stack.push_async_callback(self.mcp.close)
        self.threads = ThreadService(self)
        self.runs = RunService(self)
        # 模块按依赖顺序装配，后装配的模块可以向先装配的模块登记回调。
        for info in resolve(self.settings.modules):
            module = load(info)
            if module.setup:
                await module.setup(self)
            self.modules.append(module)
        for reference in self.settings.registrations:
            module_name, symbol = reference.split(":", 1)
            register = getattr(importlib.import_module(module_name), symbol)
            result = register(self)
            if inspect.isawaitable(result):
                await result
        return self

    async def __aenter__(self):
        """初始化失败也释放部分成功的资源；调用者不必额外处理退出流程。"""
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
        """逆序释放已获得的全部资源。"""
        await self.stack.aclose()
