"""原生模型工厂与异步治理；SDK 自身负责网络重试，模板不再叠加重试。"""

from __future__ import annotations

import asyncio
import json
from typing import Any

from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.outputs import ChatGeneration, ChatGenerationChunk, ChatResult
from pydantic import PrivateAttr

from app.core.errors import AppError
from app.core.observability import models
from app.runtime.contracts import current_run


class GovernedChatModel(BaseChatModel):
    """保持 LangChain 原生模型接口；native 暴露底层对象，治理覆盖模板返回模型的异步调用。"""

    _native: Any = PrivateAttr()
    _profile: Any = PrivateAttr()
    _semaphore: Any = PrivateAttr()
    _telemetry: Any = PrivateAttr()

    def __init__(self, native: Any, profile: Any, semaphore: Any, telemetry: Any):
        super().__init__(
            profile={
                "structured_output": "structured" in profile.capabilities,
                "tool_calling": "tools" in profile.capabilities,
            }
        )
        self._native, self._profile = native, profile
        self._semaphore, self._telemetry = semaphore, telemetry

    @property
    def native(self):
        """取得底层服务商对象；直接调用会绕过模板预算，应只用于原生参数扩展。"""
        return self._native

    @property
    def model_name(self):
        """向 LangChain Agent 工厂公开服务商模型名，用于能力选择。"""
        return self._profile.model

    @property
    def _llm_type(self):
        """提供不含秘密的模型类型标记。"""
        return self._profile.provider

    def _generate(self, messages, stop=None, run_manager=None, **kwargs):
        """拒绝在工作流事件循环中使用同步模型路径。"""
        raise AppError("async_required", "模型调用必须使用异步接口")

    def bind_tools(self, tools, *, tool_choice=None, **kwargs):
        """校验工具能力后返回同样受治理的绑定模型。"""
        if tools and "tools" not in self._profile.capabilities:
            raise AppError("unsupported_capability", "模型未声明工具调用能力")
        args = {**kwargs}
        if tool_choice is not None:
            args["tool_choice"] = tool_choice
        return GovernedChatModel(
            self._native.bind_tools(tools, **args), self._profile, self._semaphore, self._telemetry
        )

    def validate_call(self, kwargs):
        """运行期参数不得提高受治理的输出、重试和超时上限。"""
        if {
            "max_tokens",
            "max_output_tokens",
            "timeout",
            "max_retries",
            "num_predict",
        } & kwargs.keys():
            raise AppError("reserved_model_parameter", "运行参数不能覆盖模型预算")

    async def _agenerate(self, messages, stop=None, run_manager=None, **kwargs):
        """调用前检查预算，持有并发槽至完成；真实 usage 与估算分开记录。"""
        self.validate_call(kwargs)
        context = current_run.get()
        estimated = len(json.dumps([m.model_dump() for m in messages], default=str).encode()) + 32
        if context:
            context.reserve(estimated, self._profile.max_output_tokens, self._profile)
        try:
            async with self._semaphore, asyncio.timeout(self._profile.timeout_seconds):
                with self._telemetry.tracer.start_as_current_span(
                    "模型调用",
                    attributes={
                        "gen_ai.operation.name": "chat",
                        "gen_ai.provider.name": self._profile.provider,
                        "gen_ai.request.model": self._profile.model,
                    },
                ):
                    result = await self._native.ainvoke(messages, stop=stop, **kwargs)
            if context:
                context.record_usage(
                    result.usage_metadata,
                    estimated + self._profile.max_output_tokens,
                    self._profile,
                )
                await context.resources.runs.persist_usage(context)
            models.labels(self._profile.provider, "success").inc()
            return ChatResult(generations=[ChatGeneration(message=result)])
        except Exception:
            models.labels(self._profile.provider, "error").inc()
            raise

    async def _astream(self, messages, stop=None, run_manager=None, **kwargs):
        """在流结束后记录完整用量；中途取消保持未知用量且按保守预算记账。"""
        if "streaming" not in self._profile.capabilities:
            raise AppError("unsupported_capability", "模型未声明流式能力")
        self.validate_call(kwargs)
        context = current_run.get()
        estimated = len(json.dumps([m.model_dump() for m in messages], default=str).encode()) + 32
        if context:
            context.reserve(estimated, self._profile.max_output_tokens, self._profile)
        usage = None
        try:
            async with self._semaphore, asyncio.timeout(self._profile.timeout_seconds):
                with self._telemetry.tracer.start_as_current_span(
                    "模型流",
                    attributes={
                        "gen_ai.operation.name": "chat",
                        "gen_ai.provider.name": self._profile.provider,
                        "gen_ai.request.model": self._profile.model,
                    },
                ):
                    async for chunk in self._native.astream(messages, stop=stop, **kwargs):
                        if chunk.usage_metadata:
                            usage = chunk.usage_metadata
                        yield ChatGenerationChunk(message=chunk)
            models.labels(self._profile.provider, "success").inc()
        finally:
            if context:
                context.record_usage(
                    usage, estimated + self._profile.max_output_tokens, self._profile
                )
                await context.resources.runs.persist_usage(context)


class ModelRegistry:
    """按名称缓存原生模型；聊天与 Embedding 完全独立，配置缺失立即诊断。"""

    def __init__(self, settings, http, telemetry):
        self.settings, self.http, self.telemetry = settings, http, telemetry
        self._models, self._embeddings, self._llama = {}, {}, {}
        self._limits = {}

    def profile(self, name: str, *, embedding: bool = False):
        """取得已声明配置；Embedding 需要显式维度，Anthropic 不提供该接口。"""
        table = self.settings.embedding_models if embedding else self.settings.chat_models
        if name not in table:
            raise AppError("model_not_configured", "模型配置不存在", 503)
        profile = table[name]
        if embedding and (not profile.dimensions or profile.provider == "anthropic"):
            raise AppError("embedding_configuration", "向量模型必须支持 Embedding 并声明维度", 503)
        return profile

    def chat(self, name: str, required: set[str] | None = None):
        """返回 LangChain BaseChatModel；透传参数与能力按注册配置执行。"""
        p = self.profile(name)
        if required and not required <= p.capabilities:
            raise AppError("unsupported_capability", "模型能力声明不满足请求")
        if name not in self._models:
            common = dict(
                model=p.model,
                api_key=p.api_key.get_secret_value(),
                max_tokens=p.max_output_tokens,
                timeout=p.timeout_seconds,
                max_retries=p.retries,
                **p.parameters,
            )
            if p.provider == "openai":
                from langchain_openai import ChatOpenAI

                native = ChatOpenAI(
                    **common,
                    base_url=p.endpoint,
                    http_async_client=self.http_for(p),
                    stream_usage=True,
                )
            elif p.provider == "anthropic":
                from langchain_anthropic import ChatAnthropic

                native = ChatAnthropic(**common, base_url=p.endpoint, anthropic_proxy=p.proxy)
            else:
                from langchain_google_genai import ChatGoogleGenerativeAI

                native = ChatGoogleGenerativeAI(
                    **common,
                    base_url=p.endpoint,
                    client_args={"proxy": p.proxy} if p.proxy else {},
                )
            self._models[name] = GovernedChatModel(
                native, p, asyncio.Semaphore(p.concurrency), self.telemetry
            )
        return self._models[name]

    def http_for(self, profile):
        """复用服务商代理客户端；客户端由统一资源上下文负责释放。"""
        return self.http[profile.proxy or ""]

    def embedding(self, name: str):
        """返回 LangChain 原生 Embeddings；向量请求由 embed 方法提供治理。"""
        p = self.profile(name, embedding=True)
        if name not in self._embeddings:
            if p.provider == "openai":
                from langchain_openai import OpenAIEmbeddings

                kwargs = dict(
                    model=p.model,
                    api_key=p.api_key.get_secret_value(),
                    base_url=p.endpoint,
                    request_timeout=p.timeout_seconds,
                    max_retries=p.retries,
                    http_async_client=self.http_for(p),
                    **p.parameters,
                )
                if p.model.startswith("text-embedding-3"):
                    kwargs["dimensions"] = p.dimensions
                self._embeddings[name] = OpenAIEmbeddings(**kwargs)
            else:
                from langchain_google_genai import GoogleGenerativeAIEmbeddings

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
            raise AppError("embedding_dimensions", "服务商返回的向量维度不匹配")
        return vectors

    def llama_chat(self, name: str):
        """返回独立的 LlamaIndex FunctionCallingLLM，内部保留真实服务商原生对象。"""
        from app.services.llama_models import GovernedLlamaModel

        p = self.profile(name)
        if name not in self._llama:
            if p.provider == "openai":
                from llama_index.llms.openai_like import OpenAILike

                native = OpenAILike(
                    model=p.model,
                    api_key=p.api_key.get_secret_value(),
                    api_base=p.endpoint,
                    async_http_client=self.http_for(p),
                    timeout=p.timeout_seconds,
                    max_retries=p.retries,
                    max_tokens=p.max_output_tokens,
                    context_window=p.context_tokens,
                    is_chat_model=True,
                    is_function_calling_model="tools" in p.capabilities,
                    additional_kwargs=p.parameters,
                )
            elif p.provider == "anthropic":
                from llama_index.llms.anthropic import Anthropic

                native = Anthropic(
                    model=p.model,
                    api_key=p.api_key.get_secret_value(),
                    base_url=p.endpoint,
                    timeout=p.timeout_seconds,
                    max_retries=p.retries,
                    max_tokens=p.max_output_tokens,
                    additional_kwargs=p.parameters,
                )
                import anthropic

                native._aclient = anthropic.AsyncAnthropic(
                    api_key=p.api_key.get_secret_value(),
                    base_url=p.endpoint,
                    timeout=p.timeout_seconds,
                    max_retries=p.retries,
                    http_client=self.http_for(p),
                )
            else:
                from google.genai.types import HttpOptions
                from llama_index.llms.google_genai import GoogleGenAI

                native = GoogleGenAI(
                    model=p.model,
                    api_key=p.api_key.get_secret_value(),
                    max_tokens=p.max_output_tokens,
                    max_retries=p.retries,
                    context_window=p.context_tokens,
                    http_options=HttpOptions(
                        base_url=p.endpoint,
                        timeout=int(p.timeout_seconds * 1000),
                        async_client_args={"proxy": p.proxy} if p.proxy else {},
                    ),
                    **p.parameters,
                )
            self._llama[name] = GovernedLlamaModel(
                native, p, asyncio.Semaphore(p.concurrency), self.telemetry
            )
        return self._llama[name]

    def llama_embedding(self, name: str):
        """返回 LlamaIndex 原生 BaseEmbedding，模型空间与知识服务声明一致。"""
        p = self.profile(name, embedding=True)
        if p.provider == "openai":
            from llama_index.embeddings.openai import OpenAIEmbedding

            return OpenAIEmbedding(
                model=p.model,
                api_key=p.api_key.get_secret_value(),
                api_base=p.endpoint,
                async_http_client=self.http_for(p),
                timeout=p.timeout_seconds,
                max_retries=p.retries,
                dimensions=p.dimensions if p.model.startswith("text-embedding-3") else None,
                additional_kwargs=p.parameters,
            )
        from google.genai.types import EmbedContentConfig, HttpOptions
        from llama_index.embeddings.google_genai import GoogleGenAIEmbedding

        return GoogleGenAIEmbedding(
            model_name=p.model,
            api_key=p.api_key.get_secret_value(),
            embedding_config=EmbedContentConfig(output_dimensionality=p.dimensions),
            retries=p.retries,
            timeout=int(p.timeout_seconds),
            http_options=HttpOptions(
                base_url=p.endpoint, async_client_args={"proxy": p.proxy} if p.proxy else {}
            ),
            **p.parameters,
        )

    async def close(self):
        """关闭模板创建的原生 SDK 客户端，代理 HTTP 共享客户端由外层退出栈负责。"""
        visited = set()
        for wrapper in [*self._models.values(), *self._llama.values()]:
            native = wrapper.native
            for name in [
                "_async_client",
                "_aclient",
                "_client",
                "async_client",
                "root_async_client",
                "root_client",
                "client",
            ]:
                client = getattr(native, name, None)
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
