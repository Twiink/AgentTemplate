"""框架共享的注册及运行契约；保留原生对象，不强制把各框架状态变成同一种消息。"""

from __future__ import annotations

import contextvars
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any, Literal, Protocol

from pydantic import BaseModel

from app.core.errors import AppError
from app.core.security import Principal


@dataclass
class RunContext:
    """单次租约内的可信执行上下文；预算随调用累积，线程命名空间由服务器生成。"""

    principal: Principal
    run_id: str
    thread_id: str
    lease_token: str
    resources: Any
    deadline: float
    token_budget: int
    cost_budget: float | None
    usage: dict[str, Any] = field(
        default_factory=lambda: {
            "input_tokens": None,
            "output_tokens": None,
            "estimated_tokens": 0,
            "missing_usage_calls": 0,
            "cost": None,
            "calls": 0,
        }
    )
    prompt_versions: dict[str, str] = field(default_factory=dict)
    approvals: dict[str, Any] = field(default_factory=dict)
    call_counter: int = 0
    reserved_tokens: int = 0
    llama_context: Any = None

    @property
    def checkpoint_thread(self) -> str:
        """把租户、主体与线程编码为不可碰撞的命名空间，不接受原生参数覆盖。"""
        import hashlib

        return hashlib.sha256(
            f"{self.principal.tenant_id}\0{self.principal.subject_id}\0{self.thread_id}".encode()
        ).hexdigest()

    def reserve(self, estimated_input: int, max_output: int, profile: Any) -> None:
        """调用前检查剩余时间、上下文及总预算；估算与服务商 usage 分别保留。"""
        if time.monotonic() >= self.deadline:
            raise AppError("run_timeout", "运行已超过时间预算")
        if estimated_input + max_output > profile.context_tokens:
            raise AppError("context_budget", "输入超过模型上下文预算")
        used = self.usage["estimated_tokens"] + self.reserved_tokens
        if used + estimated_input + max_output > self.token_budget:
            raise AppError("token_budget", "运行 Token 预算不足")
        if self.cost_budget is not None:
            if profile.input_price_per_million is None or profile.output_price_per_million is None:
                raise AppError("pricing_required", "费用预算要求配置完整价格和版本")
            projected = (
                estimated_input * profile.input_price_per_million
                + max_output * profile.output_price_per_million
            ) / 1_000_000
            if (self.usage["cost"] or 0) + projected > self.cost_budget:
                raise AppError("cost_budget", "运行费用预算不足")
        self.reserved_tokens += estimated_input + max_output

    def record_usage(self, value: dict[str, Any] | None, estimated: int, profile: Any) -> None:
        """记录真实用量；未知值保持 None，单独计入保守估算以防无 usage 绕过预算。"""
        self.reserved_tokens = max(0, self.reserved_tokens - estimated)
        self.usage["calls"] += 1
        incoming = value.get("input_tokens") if value else None
        outgoing = value.get("output_tokens") if value else None
        if incoming is None or outgoing is None:
            self.usage["missing_usage_calls"] += 1
            self.usage["estimated_tokens"] += estimated
        else:
            self.usage["input_tokens"] = (self.usage["input_tokens"] or 0) + int(incoming)
            self.usage["output_tokens"] = (self.usage["output_tokens"] or 0) + int(outgoing)
            self.usage["estimated_tokens"] += int(incoming) + int(outgoing)
            if (
                profile.price_version
                and profile.input_price_per_million is not None
                and profile.output_price_per_million is not None
            ):
                self.usage["cost"] = (self.usage["cost"] or 0) + (
                    int(incoming) * profile.input_price_per_million
                    + int(outgoing) * profile.output_price_per_million
                ) / 1_000_000
        if self.usage["estimated_tokens"] > self.token_budget:
            raise AppError("token_budget", "运行已超过 Token 预算")


current_run: contextvars.ContextVar[RunContext | None] = contextvars.ContextVar(
    "current_run", default=None
)


@dataclass(frozen=True)
class AgentRegistration:
    """项目声明的原生实现；factory 接收 RunContext 和 Checkpointer，输入输出由 Schema 校验。"""

    id: str
    framework: Literal["langchain", "langgraph", "llamaindex"]
    factory: Callable[..., Any]
    input_schema: type[BaseModel]
    output_schema: type[BaseModel] | None = None
    capabilities: frozenset[str] = frozenset({"streaming", "resume", "cancel"})


class RuntimeAdapter(Protocol):
    """运行适配器只负责事件、快照与恢复，具体状态仍由原生实现定义。"""

    async def execute(
        self,
        registration: AgentRegistration,
        context: RunContext,
        input: dict[str, Any],
        options: dict[str, Any],
        snapshot: dict[str, Any] | None,
        resume: dict[str, Any] | None,
    ) -> Any:
        """执行或恢复原生实现，等待输入通过持久化状态表达。"""
        ...


class AgentRegistry:
    """空的显式注册表；禁止重复名称，公共 API 返回 Schema 与能力。"""

    def __init__(self):
        self._items: dict[str, AgentRegistration] = {}

    def register(self, registration: AgentRegistration) -> None:
        """接入项目实现；重复 ID 直接失败，避免 API 与 Worker 选中不同实现。"""
        if registration.id in self._items:
            raise ValueError(f"重复 Agent ID：{registration.id}")
        self._items[registration.id] = registration

    def get(self, identifier: str) -> AgentRegistration:
        """取得注册项；缺失时返回明确诊断，不运行任何默认业务。"""
        if identifier not in self._items:
            raise AppError("agent_not_registered", "Agent 未注册", 404)
        return self._items[identifier]

    def describe(self) -> list[dict[str, Any]]:
        """输出无秘密的公开注册信息。"""
        return [
            {
                "id": item.id,
                "framework": item.framework,
                "input_schema": item.input_schema.model_json_schema(),
                "output_schema": item.output_schema.model_json_schema()
                if item.output_schema
                else None,
                "capabilities": sorted(item.capabilities),
            }
            for item in self._items.values()
        ]
