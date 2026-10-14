"""运行上下文：单次租约内的可信身份与预算累积，工具与模型共享同一份。

Agent 的注册契约见 app.agents.registry；本模块只保留运行期状态。
"""

from __future__ import annotations

import contextvars
import time
from dataclasses import dataclass, field
from typing import Any

from app.core.errors import AppError, Code
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
            "estimated_cost": 0.0,
            "reserved_cost": 0.0,
            "missing_usage_calls": 0,
            "cost": None,
            "calls": 0,
            "reserved_tokens": 0,
        }
    )
    prompt_versions: dict[str, str] = field(default_factory=dict)
    approvals: dict[str, Any] = field(default_factory=dict)
    call_counter: int = 0
    reserved_tokens: int = 0
    llama_context: Any = None

    def __post_init__(self) -> None:
        """将崩溃前未结算的模型请求预留转为保守用量与费用估算。"""
        reserved = int(self.usage.get("reserved_tokens", self.reserved_tokens))
        reserved_cost = float(self.usage.get("reserved_cost", 0.0) or 0.0)
        if reserved or reserved_cost:
            self.usage["estimated_tokens"] = int(self.usage.get("estimated_tokens", 0)) + reserved
            self.usage["estimated_cost"] = (
                float(self.usage.get("estimated_cost", 0.0) or 0.0) + reserved_cost
            )
            self.usage["missing_usage_calls"] = int(self.usage.get("missing_usage_calls", 0)) + 1
            self.usage["reserved_tokens"] = 0
            self.usage["reserved_cost"] = 0.0
        self.reserved_tokens = 0

    @property
    def checkpoint_thread(self) -> str:
        """把租户、主体与线程编码为不可碰撞的命名空间，不接受原生参数覆盖。"""
        import hashlib

        return hashlib.sha256(
            f"{self.principal.tenant_id}\0{self.principal.subject_id}\0{self.thread_id}".encode()
        ).hexdigest()

    def reserve(
        self, estimated_input: int, max_output: int, profile: Any, price: Any = None
    ) -> float:
        """调用前检查剩余时间、上下文及总预算；估算与服务商 usage 分别保留。

        price 为价格表命中的单价；费用预算必须有价格，缺失时明确拒绝而不是按 0 计费。
        """
        if time.monotonic() >= self.deadline:
            raise AppError(Code.RUN_TIMEOUT, "运行已超过时间预算")
        if estimated_input + max_output > profile.context_tokens:
            raise AppError(Code.CONTEXT_BUDGET, "输入超过模型上下文预算")
        used = self.usage["estimated_tokens"] + self.reserved_tokens
        if used + estimated_input + max_output > self.token_budget:
            raise AppError(Code.TOKEN_BUDGET, "运行 Token 预算不足")
        projected = 0.0
        if price is not None:
            projected = (
                estimated_input * price.input_per_million + max_output * price.output_per_million
            ) / 1_000_000
        if self.cost_budget is not None:
            if price is None:
                raise AppError(Code.PRICING_REQUIRED, "费用预算要求价格表收录该模型")
            spent = (
                (self.usage.get("cost") or 0)
                + (self.usage.get("estimated_cost") or 0)
                + (self.usage.get("reserved_cost") or 0)
            )
            if spent + projected > self.cost_budget:
                raise AppError(Code.COST_BUDGET, "运行费用预算不足")
        self.reserved_tokens += estimated_input + max_output
        self.usage["reserved_tokens"] = self.reserved_tokens
        self.usage["reserved_cost"] = (self.usage.get("reserved_cost") or 0) + projected
        return projected

    def record_usage(
        self,
        value: dict[str, Any] | None,
        reserved: int,
        profile: Any,
        price: Any = None,
        reserved_cost: float = 0.0,
    ) -> None:
        """释放预算预留并记录真实用量；缺失时将 token 与费用预留转为估算值。"""
        self.reserved_tokens = max(0, self.reserved_tokens - reserved)
        self.usage["reserved_tokens"] = self.reserved_tokens
        self.usage["reserved_cost"] = max(
            0.0, (self.usage.get("reserved_cost") or 0) - reserved_cost
        )
        self.usage["calls"] += 1
        incoming = value.get("input_tokens") if value else None
        outgoing = value.get("output_tokens") if value else None
        if incoming is None or outgoing is None:
            self.usage["missing_usage_calls"] += 1
            self.usage["estimated_tokens"] += reserved
            if price is not None:
                self.usage["estimated_cost"] = (
                    self.usage.get("estimated_cost") or 0
                ) + reserved_cost
        else:
            self.usage["input_tokens"] = (self.usage["input_tokens"] or 0) + int(incoming)
            self.usage["output_tokens"] = (self.usage["output_tokens"] or 0) + int(outgoing)
            self.usage["estimated_tokens"] += int(incoming) + int(outgoing)
            if price is not None:
                self.usage["cost"] = (self.usage["cost"] or 0) + (
                    int(incoming) * price.input_per_million
                    + int(outgoing) * price.output_per_million
                ) / 1_000_000
        if self.usage["estimated_tokens"] > self.token_budget:
            raise AppError(Code.TOKEN_BUDGET, "运行已超过 Token 预算")


# 运行期可信上下文的传递通道；阶段 3.4 改为通过 LangChain 的 context_schema 显式传入。
current_run: contextvars.ContextVar[RunContext | None] = contextvars.ContextVar(
    "current_run", default=None
)
