"""模型调用治理的离线契约测试。"""

from __future__ import annotations

import asyncio
import time
from types import SimpleNamespace

import pytest
from langchain_core.messages import HumanMessage

from app.core.errors import AppError, Code
from app.core.security import Principal
from app.llm.gateway import ModelGateway
from app.llm.pricing import Price
from app.runtime.contracts import RunContext, current_run


class FakeRunService:
    """收集用量持久化请求，不访问数据库。"""

    def __init__(self):
        """初始化空的持久化记录列表。"""
        self.persisted = []

    async def persist_usage(self, context):
        """保存当前用量快照副本。"""
        self.persisted.append(dict(context.usage))


class FakeModel:
    """提供固定用量的聊天模型替身。"""

    def __init__(self, usage=None):
        """设置同步响应与流式响应将返回的固定用量。"""
        self.usage = usage
        self.called = False
        self.call_count = 0

    async def ainvoke(self, input, config=None, **kwargs):
        """返回带固定 usage metadata 的模型响应。"""
        self.called = True
        self.call_count += 1
        return SimpleNamespace(usage_metadata=self.usage or {})

    async def astream(self, input, config=None, **kwargs):
        """依次输出内容块及最终累计 usage metadata。"""
        self.called = True
        yield SimpleNamespace(content="answer", usage_metadata={})
        yield SimpleNamespace(
            content="",
            usage_metadata={"input_tokens": 6, "output_tokens": 3},
        )


def make_context(*, token_budget=64, cost_budget=None):
    """创建不依赖数据库的可信运行上下文。"""
    principal = Principal("tenant", "subject", frozenset())
    resources = SimpleNamespace(
        runs=FakeRunService(),
        settings=SimpleNamespace(monthly_cost_budget=None),
    )
    context = RunContext(
        principal=principal,
        run_id="run-1",
        thread_id="thread-1",
        lease_token="lease-1",
        resources=resources,
        deadline=time.monotonic() + 30,
        token_budget=token_budget,
        cost_budget=cost_budget,
    )
    return context


def make_gateway(usage=None):
    """构建使用固定价格表的网关和模型替身。"""
    gateway = ModelGateway.__new__(ModelGateway)
    gateway.price_for = lambda profile: Price(2.0, 4.0)
    model = FakeModel(usage)
    profile = SimpleNamespace(
        provider="openai",
        model="test-model",
        max_output_tokens=4,
        context_tokens=128,
    )
    return gateway, model, profile


def test_ainvoke_records_actual_usage_and_persists_it():
    """验证普通模型调用会累计实际用量并持久化。"""

    async def run():
        """执行同步调用预算与用量断言。"""
        context = make_context()
        gateway, model, profile = make_gateway({"input_tokens": 6, "output_tokens": 3})
        wrapped = gateway._wrap_with_metrics(model, "test-alias", profile)
        token = current_run.set(context)
        try:
            result = await wrapped.ainvoke([HumanMessage(content="hello")])
        finally:
            current_run.reset(token)
        assert result.usage_metadata["input_tokens"] == 6
        assert model.called
        assert context.usage["estimated_tokens"] == 9
        assert context.usage["cost"] == pytest.approx(24 / 1_000_000)
        assert context.usage["calls"] == 1
        assert len(context.resources.runs.persisted) == 2
        assert context.resources.runs.persisted[0]["reserved_tokens"] > 0
        assert context.resources.runs.persisted[-1]["reserved_tokens"] == 0

    asyncio.run(run())


def test_astream_records_usage_after_consuming_the_stream():
    """验证流式响应完成后累计服务商提供的用量。"""

    async def run():
        """消费完整流并检查最终用量。"""
        context = make_context()
        gateway, model, profile = make_gateway()
        wrapped = gateway._wrap_with_metrics(model, "test-stream", profile)
        token = current_run.set(context)
        try:
            chunks = [chunk async for chunk in wrapped.astream([HumanMessage(content="hello")])]
        finally:
            current_run.reset(token)
        assert len(chunks) == 2
        assert context.usage["input_tokens"] == 6
        assert context.usage["output_tokens"] == 3
        assert context.usage["cost"] == pytest.approx(24 / 1_000_000)
        assert context.resources.runs.persisted[0]["reserved_tokens"] > 0
        assert context.resources.runs.persisted[-1]["reserved_tokens"] == 0

    asyncio.run(run())


def test_budget_rejection_happens_before_provider_call():
    """验证 Token 预算不足时不会调用服务商。"""

    async def run():
        """验证请求在调用模型前被 Token 预算拒绝。"""
        context = make_context(token_budget=1)
        gateway, model, profile = make_gateway({"input_tokens": 1, "output_tokens": 1})
        wrapped = gateway._wrap_with_metrics(model, "test-budget", profile)
        token = current_run.set(context)
        try:
            with pytest.raises(AppError) as raised:
                await wrapped.ainvoke([HumanMessage(content="hello")])
        finally:
            current_run.reset(token)
        assert raised.value.code == Code.TOKEN_BUDGET
        assert not model.called
        assert context.usage["calls"] == 0

    asyncio.run(run())


def test_cost_budget_requires_pricing_and_rejects_before_provider_call():
    """验证单次费用预算超限时在请求前拒绝。"""

    async def run():
        """验证请求在调用模型前被费用预算拒绝。"""
        context = make_context(cost_budget=0.000001)
        gateway, model, profile = make_gateway({"input_tokens": 1, "output_tokens": 1})
        gateway.price_for = lambda configured_profile: Price(1000.0, 1000.0)
        wrapped = gateway._wrap_with_metrics(model, "test-cost-budget", profile)
        token = current_run.set(context)
        try:
            with pytest.raises(AppError) as raised:
                await wrapped.ainvoke([HumanMessage(content="hello")])
        finally:
            current_run.reset(token)
        assert raised.value.code == Code.COST_BUDGET
        assert not model.called

    asyncio.run(run())


def test_monthly_quota_requires_a_price_for_each_model():
    """验证启用月度配额时未定价模型在调用前拒绝。"""

    async def run():
        """确认未定价模型不会绕过已启用的主体月度费用控制。"""
        context = make_context()
        context.resources.settings.monthly_cost_budget = 10.0
        gateway, model, profile = make_gateway()
        gateway.price_for = lambda configured_profile: None
        wrapped = gateway._wrap_with_metrics(model, "unpriced-model", profile)
        token = current_run.set(context)
        try:
            with pytest.raises(AppError) as raised:
                await wrapped.ainvoke([HumanMessage(content="hello")])
        finally:
            current_run.reset(token)
        assert raised.value.code == Code.PRICING_REQUIRED
        assert not model.called

    asyncio.run(run())


def test_missing_usage_is_counted_conservatively():
    """验证服务商没有返回用量时使用保守估算。"""

    async def run():
        """验证缺失 usage 时采用输入输出估算值。"""
        context = make_context()
        gateway, model, profile = make_gateway({})
        wrapped = gateway._wrap_with_metrics(model, "test-estimate", profile)
        token = current_run.set(context)
        try:
            await wrapped.ainvoke([HumanMessage(content="hello")])
        finally:
            current_run.reset(token)
        assert context.usage["missing_usage_calls"] == 1
        assert context.usage["estimated_tokens"] > 0
        assert context.usage["estimated_cost"] > 0
        assert context.usage["reserved_tokens"] == 0
        assert context.usage["reserved_cost"] == 0

    asyncio.run(run())


def test_recovered_run_converts_unsettled_reservation_to_estimated_usage():
    """验证 Worker 崩溃后的未结算模型请求按预留值保守记账。"""
    context = make_context()
    context.usage.update(
        {
            "reserved_tokens": 40,
            "reserved_cost": 0.02,
            "estimated_tokens": 8,
            "estimated_cost": 0.001,
        }
    )
    recovered = RunContext(
        principal=context.principal,
        run_id=context.run_id,
        thread_id=context.thread_id,
        lease_token="recovered-lease",
        resources=context.resources,
        deadline=context.deadline,
        token_budget=context.token_budget,
        cost_budget=context.cost_budget,
        usage=dict(context.usage),
    )
    assert recovered.usage["estimated_tokens"] == 48
    assert recovered.usage["estimated_cost"] == pytest.approx(0.021)
    assert recovered.usage["reserved_tokens"] == 0
    assert recovered.usage["reserved_cost"] == 0
    assert recovered.usage["missing_usage_calls"] == 1


def test_estimated_cost_still_enforces_the_run_budget():
    """验证没有真实 usage 时，估算费用仍会限制后续模型调用。"""

    async def run():
        """连续发起调用并确认估算费用会阻止第二次请求。"""
        context = make_context(cost_budget=0.015)
        gateway, model, profile = make_gateway({})
        gateway.price_for = lambda configured_profile: Price(1000.0, 1000.0)
        wrapped = gateway._wrap_with_metrics(model, "test-estimated-cost", profile)
        token = current_run.set(context)
        try:
            await wrapped.ainvoke([HumanMessage(content="hello")])
            with pytest.raises(AppError) as raised:
                await wrapped.ainvoke([HumanMessage(content="hello")])
        finally:
            current_run.reset(token)
        assert raised.value.code == Code.COST_BUDGET
        assert model.call_count == 1

    asyncio.run(run())
