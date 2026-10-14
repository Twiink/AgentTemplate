"""API、CLI 和 Worker 共用的资源容器：先建立核心资源，再按依赖顺序装配启用的模块，退出时逆序释放。

导入本模块不会连接外部服务；连接在进入 async with 时建立。
"""

from __future__ import annotations

import asyncio
from contextlib import AsyncExitStack
from typing import TYPE_CHECKING

import httpx
from redis.asyncio import Redis

from app.agents.registry import AgentRegistry
from app.core.config import Settings
from app.core.logging import setup_logging
from app.core.telemetry import Telemetry
from app.db.repository import Repository
from app.db.session import Database
from app.llm.gateway import ModelGateway
from app.modules import Module, load, resolve
from app.prompts.loader import PromptLoader
from app.runtime.bus import BusRegistry
from app.runtime.checkpointer import CheckpointPool, FenceLock
from app.runtime.streams import StreamLog

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
        # 空注册表；启动时由 build_agents 按配置发现并构建。
        self.agents = AgentRegistry(enabled=list(settings.agents.enabled))
        self.modules: list[Module] = []
        # 检查点与 fence 各自使用独立连接池，避免长事务占用业务连接。
        self.checkpoints = CheckpointPool(settings)
        self.fence = FenceLock(settings)
        # 请求内流式输出的事件总线；跨进程续传仍以 run_events 为准。
        self.buses = BusRegistry()
        # 后台档的跨进程增量通道；Redis 故障时降级为 run_events 回放。
        self.streams = StreamLog(self)
        self.stack.push_async_callback(self.checkpoints.close)
        self.stack.push_async_callback(self.fence.close)
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

        # 初始化配额、缓存和幂等性管理器
        from app.core.cache import ResponseCache
        from app.core.idempotency import IdempotencyManager
        from app.core.quota import QuotaManager

        self.quota = QuotaManager(self.redis, self.logger, self.settings)
        self.cache = ResponseCache(self.redis, self.logger)
        self.idempotency = IdempotencyManager(self.redis, self.logger)

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

        # 任一终态都记录费用，因为失败或取消前的模型请求仍可能产生费用。
        async def record_cost_hook(session, run):
            """把运行累计费用记入主体月度配额。"""
            from app.core.security import Principal

            principal = Principal(run.tenant_id, run.subject_id, frozenset(run.scopes))
            cost = (run.usage.get("cost", run.usage.get("total_cost", 0)) or 0) + (
                run.usage.get("estimated_cost") or 0
            )
            if cost > 0:
                await self.quota.record(principal, cost, run_id=run.id)

        self.runs.terminal_hooks.append(record_cost_hook)

        # 模块按依赖顺序装配，后装配的模块可以向先装配的模块登记回调。
        for info in resolve(self.settings.modules):
            module = load(info)
            if module.setup:
                await module.setup(self)
            self.modules.append(module)
        # Agent 在模块装配完成后统一构建；任何一个构建失败都会停止启动。
        await self.build_agents()
        return self

    async def build_agents(self):
        """发现并构建启用的 Agent；构建只做装配，运行期身份由服务端注入。

        没有任何启用的 Agent 时不建立 Checkpointer 连接，保持 minimal 档只依赖
        PostgreSQL 可用即可启动；有 Agent 时才解析共享的 Checkpointer。
        """
        from app.agents.registry import AgentRegistry, BuildContext

        self.agents = AgentRegistry(enabled=list(self.settings.agents.enabled))
        specs = self.agents.discover()
        if not specs:
            self.agents.items = {}
            return
        required_mcp = dict.fromkeys(server for spec in specs for server in spec.mcp_servers)
        for server in required_mcp:
            await self.mcp.connect(server)
        context = BuildContext(
            settings=self.settings,
            models=self.models,
            tools=self.tools,
            prompts=self.prompts,
            checkpointer=await self.checkpoint(),
            store=self.memories.store if self.memories is not None else None,
        )
        await self.agents.build_all(context)

    async def __aenter__(self):
        """初始化失败也释放部分成功的资源；调用者不必额外处理退出流程。"""
        try:
            return await self._open()
        except BaseException:
            await self.stack.aclose()
            raise

    async def checkpoint(self):
        """返回基于独立连接池的 Saver；连接在首次使用时建立，迁移只在 CLI 中执行。

        与旧实现不同，这里不再用单连接承载全部检查点读写，并发运行不会互相排队。
        """
        return await self.checkpoints.saver_instance()

    async def __aexit__(self, *args):
        """逆序释放已获得的全部资源。"""
        await self.stack.aclose()
