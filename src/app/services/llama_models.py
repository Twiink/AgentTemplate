"""LlamaIndex 原生模型治理；异步接口调用真实 SDK，保留原生工具与结构化输出能力。"""

from __future__ import annotations

import asyncio
import json
from typing import Any

from llama_index.core.base.llms.types import ChatMessage, CompletionResponse, MessageRole
from llama_index.core.llms.function_calling import FunctionCallingLLM
from pydantic import PrivateAttr

from app.core.errors import AppError
from app.core.observability import models
from app.runtime.contracts import current_run


class GovernedLlamaModel(FunctionCallingLLM):
    """包装原生 FunctionCallingLLM；统一模型调用预算及并发，不转换 Workflow 状态。"""

    _native: Any = PrivateAttr()
    _profile: Any = PrivateAttr()
    _limit: Any = PrivateAttr()
    _telemetry: Any = PrivateAttr()

    def __init__(self, native, profile, limit, telemetry):
        super().__init__()
        self._native, self._profile, self._limit, self._telemetry = (
            native,
            profile,
            limit,
            telemetry,
        )

    @property
    def native(self):
        """原生服务商对象；直接调用不经过模板治理。"""
        return self._native

    @property
    def metadata(self):
        """公开原生模型元数据，用于 AgentWorkflow 原生能力判定。"""
        return self._native.metadata

    def _prepare_chat_with_tools(
        self,
        tools,
        user_msg=None,
        chat_history=None,
        verbose=False,
        allow_parallel_tool_calls=False,
        tool_required=False,
        **kwargs,
    ):
        """工具能力先校验，再使用原生格式构造工具参数。"""
        if "tools" not in self._profile.capabilities:
            raise AppError("unsupported_capability", "模型未声明工具调用能力")
        return self._native._prepare_chat_with_tools(
            tools,
            user_msg=user_msg,
            chat_history=chat_history,
            verbose=verbose,
            allow_parallel_tool_calls=allow_parallel_tool_calls,
            tool_required=tool_required,
            **kwargs,
        )

    def get_tool_calls_from_response(self, response, error_on_no_tool_call=True, **kwargs):
        """由服务商原生模型解析工具调用，保留原始调用 ID。"""
        return self._native.get_tool_calls_from_response(
            response, error_on_no_tool_call=error_on_no_tool_call, **kwargs
        )

    def chat(self, messages, **kwargs):
        """拒绝同步网络路径，避免阻塞 Worker 事件循环。"""
        raise AppError("async_required", "模型调用必须使用异步接口")

    def complete(self, prompt, formatted=False, **kwargs):
        """同步补全不受支持。"""
        raise AppError("async_required", "模型调用必须使用异步接口")

    def stream_chat(self, messages, **kwargs):
        """同步流不受支持。"""
        raise AppError("async_required", "模型调用必须使用异步接口")

    def stream_complete(self, prompt, formatted=False, **kwargs):
        """同步补全流不受支持。"""
        raise AppError("async_required", "模型调用必须使用异步接口")

    def usage(self, response):
        """提取真实服务商 usage；缺失字段保持未知，不以零填充。"""
        raw = response.raw
        if hasattr(raw, "model_dump"):
            raw = raw.model_dump()
        raw = raw if isinstance(raw, dict) else {}
        usage = raw.get("usage") or raw.get("usage_metadata") or raw.get("usageMetadata") or {}
        incoming = usage.get(
            "prompt_tokens",
            usage.get(
                "input_tokens", usage.get("prompt_token_count", usage.get("promptTokenCount"))
            ),
        )
        outgoing = usage.get(
            "completion_tokens",
            usage.get(
                "output_tokens",
                usage.get("candidates_token_count", usage.get("candidatesTokenCount")),
            ),
        )
        return (
            {"input_tokens": incoming, "output_tokens": outgoing}
            if incoming is not None and outgoing is not None
            else None
        )

    async def achat(self, messages, **kwargs):
        """调用前校验保守输入与输出预算，完成后记录真实用量与费用。"""
        if {
            "max_tokens",
            "max_output_tokens",
            "timeout",
            "max_retries",
            "num_predict",
        } & kwargs.keys():
            raise AppError("reserved_model_parameter", "运行参数不能覆盖模型预算")
        context = current_run.get()
        estimated = len(json.dumps([m.model_dump() for m in messages], default=str).encode()) + 32
        if context:
            context.reserve(estimated, self._profile.max_output_tokens, self._profile)
        async with self._limit, asyncio.timeout(self._profile.timeout_seconds):
            with self._telemetry.tracer.start_as_current_span(
                "LlamaIndex 模型调用",
                attributes={
                    "gen_ai.operation.name": "chat",
                    "gen_ai.provider.name": self._profile.provider,
                    "gen_ai.request.model": self._profile.model,
                },
            ):
                response = await self._native.achat(messages, **kwargs)
        if context:
            context.record_usage(
                self.usage(response), estimated + self._profile.max_output_tokens, self._profile
            )
            await context.resources.runs.persist_usage(context)
        models.labels(self._profile.provider, "success").inc()
        return response

    async def acomplete(self, prompt, formatted=False, **kwargs):
        """把文本补全表达为原生聊天消息，输出 LlamaIndex CompletionResponse。"""
        response = await self.achat([ChatMessage(role=MessageRole.USER, content=prompt)], **kwargs)
        return CompletionResponse(text=response.message.content or "", raw=response.raw)

    async def astream_chat(self, messages, **kwargs):
        """保持原生异步流，流结束或取消后记录用量；并发槽一直持有至流关闭。"""
        if {
            "max_tokens",
            "max_output_tokens",
            "timeout",
            "max_retries",
            "num_predict",
        } & kwargs.keys():
            raise AppError("reserved_model_parameter", "运行参数不能覆盖模型预算")
        context = current_run.get()
        estimated = len(json.dumps([m.model_dump() for m in messages], default=str).encode()) + 32
        if context:
            context.reserve(estimated, self._profile.max_output_tokens, self._profile)

        async def stream():
            """把原生异步事件逐项返回，并确保退出流时完成资源治理。"""
            usage = None
            try:
                async with self._limit, asyncio.timeout(self._profile.timeout_seconds):
                    generator = await self._native.astream_chat(messages, **kwargs)
                    async for response in generator:
                        usage = self.usage(response) or usage
                        yield response
                models.labels(self._profile.provider, "success").inc()
            finally:
                if context:
                    context.record_usage(
                        usage, estimated + self._profile.max_output_tokens, self._profile
                    )
                    await context.resources.runs.persist_usage(context)

        return stream()

    async def astream_complete(self, prompt, formatted=False, **kwargs):
        """输出原生 CompletionResponse 异步流。"""
        source = await self.astream_chat(
            [ChatMessage(role=MessageRole.USER, content=prompt)], **kwargs
        )

        async def stream():
            """把原生异步事件逐项返回，并确保退出流时完成资源治理。"""
            async for response in source:
                yield CompletionResponse(
                    text=response.message.content or "", delta=response.delta, raw=response.raw
                )

        return stream()
