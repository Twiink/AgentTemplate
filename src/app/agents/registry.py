"""Agent 注册契约：项目在 app.agents.<名称>.agent 中导出 spec，启动时自动发现并构建一次。

构建只做装配，不发起网络调用；任何一个 Agent 构建失败都停止启动，避免运行时才暴露
装配错误。运行时的可信身份由服务端写入运行上下文，客户端和模型都不能覆盖。
"""

from __future__ import annotations

import asyncio
import importlib
import inspect
import pkgutil
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from pydantic import BaseModel

from app.core.errors import AppError

if TYPE_CHECKING:
    from langgraph.graph.state import CompiledStateGraph

    from app.core.config import Settings
    from app.llm.gateway import ModelGateway
    from app.prompts.loader import PromptLoader
    from app.tools.registry import ToolRegistry


@dataclass(frozen=True)
class AgentSpec:
    """项目声明的 Agent；build 只装配，不发起网络调用。"""

    id: str
    description: str
    build: Callable[[BuildContext], CompiledStateGraph | Awaitable[CompiledStateGraph]]
    # 缺省表示直接接收 messages 输入。
    input_schema: type[BaseModel] | None = None
    # 在模板运行上下文之外追加的字段；身份字段由服务端写入，不接受这里的覆盖。
    context_schema: type | None = None


@dataclass(frozen=True)
class BuildContext:
    """构建期可用资源；只暴露装配所需的句柄，不暴露请求期状态。"""

    settings: Settings
    models: ModelGateway
    tools: ToolRegistry
    prompts: PromptLoader
    checkpointer: Any
    store: Any = None
    middleware: Any = None


@dataclass
class RegisteredAgent:
    """已构建的 Agent；graph 为 LangGraph 编译结果，供运行时直接执行。"""

    spec: AgentSpec
    graph: Any = None
    built: bool = False


@dataclass
class AgentRegistry:
    """按配置发现、校验并构建 Agent；ID 重复或构建失败都会停止启动。"""

    enabled: list[str] = field(default_factory=list)
    items: dict[str, RegisteredAgent] = field(default_factory=dict)

    def discover(self) -> list[AgentSpec]:
        """扫描 app.agents.*.agent 中的 spec；ID 重复时直接失败。"""
        import app.agents as package

        found: dict[str, AgentSpec] = {}
        for module in pkgutil.walk_packages(package.__path__, prefix="app.agents."):
            if not module.name.endswith(".agent"):
                continue
            loaded = importlib.import_module(module.name)
            spec = getattr(loaded, "spec", None)
            if spec is None:
                continue
            if not isinstance(spec, AgentSpec):
                raise AppError("agent_spec_invalid", f"{module.name} 的 spec 不是 AgentSpec")
            if spec.id in found:
                raise AppError("agent_id_duplicate", f"重复的 Agent ID：{spec.id}")
            found[spec.id] = spec
        # enabled 非空时只保留列出的 Agent；未列出的不构建，也不出现在 /agents 中。
        if self.enabled:
            missing = [name for name in self.enabled if name not in found]
            if missing:
                raise AppError("agent_not_found", "配置启用的 Agent 不存在：" + "、".join(missing))
            found = {name: spec for name, spec in found.items() if name in self.enabled}
        return list(found.values())

    async def build_all(self, context: BuildContext) -> None:
        """启动时构建所有启用的 Agent；构建失败直接抛出，让进程停止启动。"""
        specs = self.discover()
        self.items = {spec.id: RegisteredAgent(spec=spec) for spec in specs}
        results = await asyncio.gather(
            *(self._build(item, context) for item in self.items.values()),
            return_exceptions=True,
        )
        for item, result in zip(self.items.values(), results, strict=True):
            if isinstance(result, BaseException):
                raise AppError(
                    "agent_build_failed", f"Agent {item.spec.id} 构建失败：{type(result).__name__}"
                ) from result
            item.graph, item.built = result, True

    async def _build(self, item: RegisteredAgent, context: BuildContext):
        """执行单个 spec 的构建函数；允许同步或异步实现。"""
        graph = item.spec.build(context)
        if inspect.isawaitable(graph):
            graph = await graph
        return graph

    def get(self, identifier: str) -> RegisteredAgent:
        """取得已构建的 Agent；缺失时返回明确诊断，不运行任何默认业务。"""
        if identifier not in self.items:
            raise AppError("agent_not_registered", "Agent 未注册", 404)
        return self.items[identifier]

    def describe(self) -> list[dict[str, Any]]:
        """输出无秘密的公开注册信息；没有 input_schema 时对外表达为 messages 输入。"""
        return [
            {
                "id": item.spec.id,
                "description": item.spec.description,
                "input_schema": item.spec.input_schema.model_json_schema()
                if item.spec.input_schema
                else None,
            }
            for item in self.items.values()
        ]
