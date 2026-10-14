"""模型网关：统一 init_chat_model 接入，支持别名白名单、可选密钥与价格表。

初始化只做装配，不发起网络调用；密钥、超时、重试等受治理字段由网关统一注入，
配置中的 parameters 不能覆盖它们。
"""

from __future__ import annotations

import asyncio
import time
from typing import Any, cast

from langchain.chat_models import init_chat_model
from langchain_core.language_models.chat_models import BaseChatModel

from app.core.errors import AppError, Code
from app.core.telemetry import (
    llm_cost_total,
    llm_request_duration_seconds,
    llm_requests_total,
    llm_time_to_first_token_seconds,
    llm_tokens_total,
)
from app.llm.pricing import PricingTable, load_pricing, require_price
from app.llm.tokens import estimate_messages
from app.runtime.contracts import current_run

# 模板内的 provider 取值到 init_chat_model 原生 provider 的映射。
# openai_compatible 复用 openai 客户端并通过 base_url 指向兼容端点，
# init_chat_model 的 _BUILTIN_PROVIDERS 中没有该名字，不能直接透传。
PROVIDER_ALIASES = {
    "openai": "openai",
    "openai_compatible": "openai",
    "anthropic": "anthropic",
    "gemini": "google_genai",
}


class ModelGateway:
    """模型注册表：按别名初始化聊天与向量模型，并提供价格查询。"""

    def __init__(self, settings, http, telemetry):
        self.settings, self.http, self.telemetry = settings, http, telemetry
        self._models, self._embeddings = {}, {}
        self._limits: dict[str, asyncio.Semaphore] = {}
        self.pricing: PricingTable = load_pricing(settings.llm.pricing_file)

    def profile(self, name: str, *, embedding: bool = False):
        """取得已声明配置；别名不在白名单内时拒绝，不做隐式回退。"""
        table = self.settings.llm.embeddings if embedding else self.settings.llm.models
        if name not in table:
            raise AppError(Code.MODEL_NOT_CONFIGURED, "模型别名未在配置中声明", 503)
        profile = table[name]
        if embedding and (not profile.dimensions or profile.provider == "anthropic"):
            raise AppError(
                Code.EMBEDDING_CONFIGURATION, "向量模型必须支持 Embedding 并声明维度", 503
            )
        return profile

    def price_for(self, profile):
        """按模型配置查询单价；provider 先做兼容别名归一，未收录时返回 None。

        声明了 price_version 的模型必须能在价格表命中，否则视为配置错误。
        """
        provider = "openai" if profile.provider == "openai_compatible" else profile.provider
        if profile.price_version is not None:
            return require_price(self.pricing, provider, profile.model, profile.price_version)
        return self.pricing.get(provider, profile.model)

    def price(self, name: str, *, embedding: bool = False):
        """按别名查询单价；用于预算检查与调用后计费。"""
        return self.price_for(self.profile(name, embedding=embedding))

    def status(self) -> list[dict[str, Any]]:
        """列出每个别名的配置状态；预检据此逐项输出，不发起收费调用。"""
        items = []
        for kind, table in (
            ("chat", self.settings.llm.models),
            ("embedding", self.settings.llm.embeddings),
        ):
            for name, profile in table.items():
                provider = "openai" if profile.provider == "openai_compatible" else profile.provider
                items.append(
                    {
                        "alias": name,
                        "kind": kind,
                        "provider": profile.provider,
                        "model": profile.model,
                        "has_key": profile.api_key is not None,
                        "priced": self.pricing.get(provider, profile.model) is not None,
                    }
                )
        return items

    def chat(self, name: str, required: set[str] | None = None):
        """返回 LangChain BaseChatModel；使用 init_chat_model 初始化，中间件治理接管预算。"""
        p = self.profile(name)
        if required and not required <= p.capabilities:
            raise AppError(Code.UNSUPPORTED_CAPABILITY, "模型能力声明不满足请求")
        if name not in self._models:
            kwargs: dict[str, Any] = {
                "model": p.model,
                "model_provider": PROVIDER_ALIASES[p.provider],
                "max_tokens": p.max_output_tokens,
                "timeout": p.timeout_seconds,
                "max_retries": p.retries,
            }
            # 兼容接口通过 base_url 指向本地端点；只有声明了密钥才注入，本地服务无需凭据。
            if p.endpoint:
                kwargs["base_url"] = p.endpoint
            if p.api_key:
                kwargs["api_key"] = p.api_key.get_secret_value()
            kwargs.update(p.parameters)

            base_model = init_chat_model(**kwargs)
            self._models[name] = self._wrap_with_metrics(base_model, name, p)
        return self._models[name]

    def _wrap_with_metrics(self, model: BaseChatModel, alias: str, profile) -> BaseChatModel:
        """包装模型以采集调用指标；记录请求、耗时、首 token 延迟、token 用量与费用。"""
        original_ainvoke = model.ainvoke
        original_astream = model.astream

        async def reserve_budget(input):
            """预留预算并在网络调用前持久化；普通模型调用保持可独立使用。"""
            context = current_run.get()
            if context is None:
                return None, 0, None, 0.0
            messages = input.get("messages") if isinstance(input, dict) else input
            if not isinstance(messages, (list, tuple)):
                messages = [messages]
            estimated = estimate_messages(messages)
            price = self.price_for(profile)
            if context.resources.settings.monthly_cost_budget is not None and price is None:
                raise AppError(
                    Code.PRICING_REQUIRED,
                    "月度费用配额要求价格表收录每个实际使用的模型",
                )
            reserved_cost = context.reserve(estimated, profile.max_output_tokens, profile, price)
            await context.resources.runs.persist_usage(context)
            return context, estimated + profile.max_output_tokens, price, reserved_cost

        async def record_budget(context, estimated, price, reserved_cost, usage):
            """累计模型用量并立即持久化，使 Worker 恢复后继续使用同一预算。"""
            if context is None:
                return
            try:
                context.record_usage(usage, estimated, profile, price, reserved_cost)
            finally:
                await context.resources.runs.persist_usage(context)

        async def tracked_ainvoke(input, config=None, **kwargs):
            """包装一次非流式调用，执行预算预留、用量持久化和请求追踪。"""
            provider = "openai" if profile.provider == "openai_compatible" else profile.provider
            started = time.monotonic()
            run_context, estimated, price, reserved_cost = await reserve_budget(input)
            usage_recorded = False

            # 创建 OTel span 并设置 GenAI 语义约定属性
            from opentelemetry import trace

            tracer = trace.get_tracer("agent-template.llm")

            with tracer.start_as_current_span(
                f"llm.{alias}",
                attributes={
                    "gen_ai.system": provider,
                    "gen_ai.request.model": alias,
                    "gen_ai.operation.name": "chat",
                },
            ) as span:
                try:
                    result = await original_ainvoke(input, config, **kwargs)
                    elapsed = time.monotonic() - started
                    llm_requests_total.labels(alias, provider, "success").inc()
                    llm_request_duration_seconds.labels(alias, provider).observe(elapsed)

                    usage = getattr(result, "usage_metadata", None) or {}
                    if run_context is not None:
                        usage_recorded = True
                        await record_budget(run_context, estimated, price, reserved_cost, usage)
                    input_tokens = usage.get("input_tokens", 0)
                    output_tokens = usage.get("output_tokens", 0)
                    if input_tokens:
                        llm_tokens_total.labels(alias, "input").inc(input_tokens)
                        span.set_attribute("gen_ai.usage.input_tokens", input_tokens)
                    if output_tokens:
                        llm_tokens_total.labels(alias, "output").inc(output_tokens)
                        span.set_attribute("gen_ai.usage.output_tokens", output_tokens)

                    if price and (input_tokens or output_tokens):
                        cost = (
                            price.input_per_million * input_tokens
                            + price.output_per_million * output_tokens
                        ) / 1_000_000
                        llm_cost_total.labels(alias).inc(cost)
                        span.set_attribute("gen_ai.usage.cost", cost)

                    span.set_status(trace.Status(trace.StatusCode.OK))
                    return result
                except BaseException as error:
                    if run_context is not None and not usage_recorded:
                        usage_recorded = True
                        await record_budget(run_context, estimated, price, reserved_cost, None)
                    elapsed = time.monotonic() - started
                    llm_requests_total.labels(alias, provider, "error").inc()
                    llm_request_duration_seconds.labels(alias, provider).observe(elapsed)
                    span.set_status(trace.Status(trace.StatusCode.ERROR, str(error)))
                    span.record_exception(error)
                    raise

        async def tracked_astream(input, config=None, **kwargs):
            """包装流式模型调用，在响应完成或中断时持久化用量。"""
            provider = "openai" if profile.provider == "openai_compatible" else profile.provider
            started = time.monotonic()
            ttft = None
            input_tokens = 0
            output_tokens = 0
            usage_seen = False
            run_context, estimated, price, reserved_cost = await reserve_budget(input)
            usage_recorded = False

            from opentelemetry import trace

            tracer = trace.get_tracer("agent-template.llm")

            with tracer.start_as_current_span(
                f"llm.{alias}.stream",
                attributes={
                    "gen_ai.system": provider,
                    "gen_ai.request.model": alias,
                    "gen_ai.operation.name": "chat.stream",
                },
            ) as span:
                try:
                    async for chunk in original_astream(input, config, **kwargs):
                        if ttft is None:
                            ttft = time.monotonic() - started
                            llm_time_to_first_token_seconds.labels(alias, provider).observe(ttft)
                            span.set_attribute("gen_ai.usage.time_to_first_token", ttft)

                        usage = getattr(chunk, "usage_metadata", None) or {}
                        usage_seen = usage_seen or bool(usage)
                        input_tokens = usage.get("input_tokens", 0) or input_tokens
                        output_tokens = usage.get("output_tokens", 0) or output_tokens

                        yield chunk

                    elapsed = time.monotonic() - started
                    llm_requests_total.labels(alias, provider, "success").inc()
                    llm_request_duration_seconds.labels(alias, provider).observe(elapsed)

                    if run_context is not None:
                        usage_recorded = True
                        await record_budget(
                            run_context,
                            estimated,
                            price,
                            reserved_cost,
                            {"input_tokens": input_tokens, "output_tokens": output_tokens}
                            if usage_seen
                            else None,
                        )

                    if input_tokens:
                        llm_tokens_total.labels(alias, "input").inc(input_tokens)
                        span.set_attribute("gen_ai.usage.input_tokens", input_tokens)
                    if output_tokens:
                        llm_tokens_total.labels(alias, "output").inc(output_tokens)
                        span.set_attribute("gen_ai.usage.output_tokens", output_tokens)

                    if price and (input_tokens or output_tokens):
                        cost = (
                            price.input_per_million * input_tokens
                            + price.output_per_million * output_tokens
                        ) / 1_000_000
                        llm_cost_total.labels(alias).inc(cost)
                        span.set_attribute("gen_ai.usage.cost", cost)

                    span.set_status(trace.Status(trace.StatusCode.OK))

                except BaseException as error:
                    if run_context is not None and not usage_recorded:
                        usage_recorded = True
                        await record_budget(run_context, estimated, price, reserved_cost, None)
                    elapsed = time.monotonic() - started
                    llm_requests_total.labels(alias, provider, "error").inc()
                    llm_request_duration_seconds.labels(alias, provider).observe(elapsed)
                    span.set_status(trace.Status(trace.StatusCode.ERROR, str(error)))
                    span.record_exception(error)
                    raise

        # LangChain 的模型接口由实例动态包装；setattr 保留原方法描述符的运行时调用方式。
        instrumented_model = cast(Any, model)
        instrumented_model.ainvoke = tracked_ainvoke
        instrumented_model.astream = tracked_astream
        return model

    def http_for(self, profile):
        """复用服务商代理客户端；客户端由统一资源上下文负责释放。"""
        return self.http[profile.proxy or ""]

    def embedding(self, name: str):
        """返回 LangChain 原生 Embeddings；向量请求由 embed 方法提供治理。"""
        p = self.profile(name, embedding=True)
        if name not in self._embeddings:
            if p.provider in {"openai", "openai_compatible"}:
                from langchain_openai import OpenAIEmbeddings

                kwargs = dict(
                    model=p.model,
                    base_url=p.endpoint,
                    request_timeout=p.timeout_seconds,
                    max_retries=p.retries,
                    http_async_client=self.http_for(p),
                    **p.parameters,
                )
                if p.api_key:
                    kwargs["api_key"] = p.api_key.get_secret_value()
                if p.model.startswith("text-embedding-3"):
                    kwargs["dimensions"] = p.dimensions
                self._embeddings[name] = OpenAIEmbeddings(**kwargs)
            else:
                from langchain_google_genai import GoogleGenerativeAIEmbeddings

                if not p.api_key:
                    raise AppError(Code.MODEL_NOT_CONFIGURED, "Gemini 向量模型必须配置密钥", 503)
                self._embeddings[name] = GoogleGenerativeAIEmbeddings(
                    model=p.model,
                    google_api_key=p.api_key.get_secret_value(),
                    output_dimensionality=p.dimensions,
                    **p.parameters,
                )
            self._limits[name] = asyncio.Semaphore(p.concurrency)
        return self._embeddings[name]

    async def embed(self, name: str, texts: list[str]) -> list[list[float]]:
        """受控异步向量调用；实际返回维度必须匹配已发布配置，不能静默截断。"""
        p = self.profile(name, embedding=True)
        model = self.embedding(name)
        async with self._limits[name], asyncio.timeout(p.timeout_seconds):
            vectors = await model.aembed_documents(texts)
        if len(vectors) != len(texts) or any(len(v) != p.dimensions for v in vectors):
            raise AppError(Code.EMBEDDING_DIMENSIONS, "服务商返回的向量维度不匹配")
        return vectors

    async def close(self):
        """关闭模板创建的原生 SDK 客户端；代理 HTTP 共享客户端由外层退出栈负责。"""
        visited = set()
        for model in self._models.values():
            if not isinstance(model, BaseChatModel):
                continue
            for name in [
                "_async_client",
                "_aclient",
                "_client",
                "async_client",
                "root_async_client",
                "root_client",
                "client",
            ]:
                client = getattr(model, name, None)
                if client is None or id(client) in visited:
                    continue
                visited.add(id(client))
                close = getattr(client, "aclose", None) or getattr(client, "close", None)
                if callable(close):
                    import inspect

                    try:
                        result = close()
                        if inspect.isawaitable(result):
                            await result
                    except Exception:
                        self.telemetry.tracer.start_span("客户端关闭失败").end()
