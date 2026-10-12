"""模型网关：统一 init_chat_model 接入，支持别名、可选密钥与价格配置。

阶段 3.1：移除 GovernedChatModel，使用 init_chat_model；LlamaIndex 适配保留到 3.2。
阶段 3.4：中间件治理接入后，本模块只负责模型初始化与定价。
"""

from __future__ import annotations

import asyncio
from typing import Any

from langchain.chat_models import init_chat_model
from langchain_core.language_models.chat_models import BaseChatModel

from app.core.errors import AppError
from app.llm.tokens import estimate_messages


class ModelGateway:
    """模型注册表：init_chat_model 统一初始化，LlamaIndex 适配保留到 3.2 删除。"""

    def __init__(self, settings, http, telemetry):
        self.settings, self.http, self.telemetry = settings, http, telemetry
        self._models, self._embeddings, self._llama = {}, {}, {}
        self._limits = {}

    def profile(self, name: str, *, embedding: bool = False):
        """取得已声明配置；Embedding 需要显式维度。"""
        table = self.settings.embedding_models if embedding else self.settings.chat_models
        if name not in table:
            raise AppError("model_not_configured", "模型配置不存在", 503)
        profile = table[name]
        if embedding and (not profile.dimensions or profile.provider == "anthropic"):
            raise AppError("embedding_configuration", "向量模型必须支持 Embedding 并声明维度", 503)
        return profile

    def chat(self, name: str, required: set[str] | None = None):
        """返回 LangChain BaseChatModel；使用 init_chat_model 初始化，中间件治理接管预算。"""
        p = self.profile(name)
        if required and not required <= p.capabilities:
            raise AppError("unsupported_capability", "模型能力声明不满足请求")
        if name not in self._models:
            # init_chat_model 的 model_provider 参数格式：provider:model
            kwargs = {
                "model": p.model,
                "model_provider": p.provider,
                "max_tokens": p.max_output_tokens,
                "timeout": p.timeout_seconds,
                "max_retries": p.retries,
            }
            if p.endpoint and p.provider == "openai":
                kwargs["base_url"] = p.endpoint
            if p.api_key:
                kwargs["api_key"] = p.api_key.get_secret_value()
            # parameters 透传给底层提供商
            kwargs.update(p.parameters)

            self._models[name] = init_chat_model(**kwargs)
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

    # ============ LlamaIndex 适配：阶段 3.2 删除 ============

    def llama_chat(self, name: str):
        """返回独立的 LlamaIndex FunctionCallingLLM；阶段 3.2 删除。"""
        from app.llm.llama import GovernedLlamaModel

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
        """返回 LlamaIndex 原生 BaseEmbedding；阶段 3.2 删除。"""
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
        """关闭模板创建的原生 SDK 客户端；代理 HTTP 共享客户端由外层退出栈负责。"""
        visited = set()
        for wrapper in [*self._models.values(), *self._llama.values()]:
            native = wrapper if isinstance(wrapper, BaseChatModel) else wrapper.native
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
