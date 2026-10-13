"""工具注册与执行治理：参数校验、权限、写操作确认、超时、并发、审计和副作用账本。

注册不等于授权给模型；Agent 通过 langchain() 或 llama() 显式绑定需要的工具。
"""

from __future__ import annotations

import asyncio
import hashlib
import inspect
import json
from collections.abc import Callable
from dataclasses import dataclass
from functools import partial
from time import monotonic
from typing import Any

from pydantic import BaseModel
from sqlalchemy import select

from app.core.errors import AppError, Code
from app.core.telemetry import tool_calls_total, tool_duration_seconds
from app.db.models import ToolCall
from app.runtime.contracts import current_run
from app.tools.builtin import files, http, search, sql, time


@dataclass(frozen=True)
class ToolDefinition:
    """工具定义与权限声明；write 默认触发确认，幂等性由稳定调用 ID 账本保证。"""

    name: str
    description: str
    schema: type[BaseModel]
    function: Callable[..., Any]
    scope: str
    write: bool = False
    omit_unset: bool = False


class ToolRegistry:
    """统一参数校验、权限、确认、超时、并发、审计和副作用账本。"""

    def __init__(self, resources):
        self.resources = resources
        self.config = resources.settings.tools
        self.limit = asyncio.Semaphore(self.config.concurrency)
        self.items: dict[str, ToolDefinition] = {}
        self.register(
            ToolDefinition(
                "file_read",
                "读取工作目录内文本文件",
                files.ReadFile,
                partial(files.read_file, self.config),
                "tools:files:read",
            )
        )
        self.register(
            ToolDefinition(
                "file_write",
                "写入工作目录内文本文件，需要确认",
                files.WriteFile,
                partial(files.write_file, self.config),
                "tools:files:write",
                True,
            )
        )
        self.register(
            ToolDefinition(
                "file_search",
                "搜索工作目录内文件名及文本",
                files.SearchFiles,
                partial(files.search_files, self.config),
                "tools:files:read",
            )
        )
        self.register(
            ToolDefinition(
                "http_fetch",
                "获取允许域名的网页正文并提取为 Markdown",
                http.FetchURL,
                partial(http.fetch, self.config),
                "tools:http",
            )
        )
        self.register(
            ToolDefinition(
                "web_search",
                "通过 Tavily 搜索公开网页",
                search.SearchWeb,
                partial(search.web_search, self.config, resources.http[""]),
                "tools:search",
            )
        )
        self.register(
            ToolDefinition(
                "sql_read",
                "使用专用只读连接执行查询",
                sql.ReadSQL,
                partial(sql.sql_read, self.config),
                "tools:sql",
            )
        )
        self.register(
            ToolDefinition(
                "current_time",
                "获取指定时区的当前时间",
                time.CurrentTimeParams,
                time.current_time,
                "tools:time",
            )
        )
        self.register(
            ToolDefinition(
                "format_time",
                "格式化 Unix 时间戳为指定格式",
                time.FormatTimeParams,
                time.format_time,
                "tools:time",
            )
        )
        self.register(
            ToolDefinition(
                "parse_time",
                "解析时间字符串为 Unix 时间戳",
                time.ParseTimeParams,
                time.parse_time,
                "tools:time",
            )
        )

    def register(self, definition: ToolDefinition) -> None:
        """注册真实实现；重复名称直接失败。"""
        if definition.name in self.items:
            raise ValueError(f"重复工具：{definition.name}")
        self.items[definition.name] = definition

    async def execute(self, name: str, arguments: dict[str, Any], *, call_id: str | None = None):
        """执行已绑定工具；副作用先记录为 executing，崩溃后保留 unknown，不盲目重放。"""
        if name not in self.items:
            raise AppError(Code.TOOL_NOT_FOUND, "工具未注册", 404)
        definition = self.items[name]
        context = current_run.get()
        if not context:
            raise AppError(Code.RUN_CONTEXT_REQUIRED, "工具必须在可信运行上下文中调用", 403)
        context.principal.require(definition.scope)
        values = definition.schema.model_validate(arguments).model_dump(
            exclude_unset=definition.omit_unset
        )
        digest = hashlib.sha256(
            json.dumps(values, sort_keys=True, default=str).encode()
        ).hexdigest()
        stable_id = call_id or digest
        if definition.write:
            decision = (
                context.approvals.get(stable_id)
                if self.config.confirm_writes
                else {"approved": True}
            )
            if decision is None:
                if context.llama_context is not None:
                    from workflows.events import HumanResponseEvent, InputRequiredEvent

                    response = await context.llama_context.wait_for_event(
                        HumanResponseEvent,
                        waiter_event=InputRequiredEvent(
                            prefix="工具写操作需要确认",
                            tool=name,
                            call_id=stable_id,
                            arguments_hash=digest,
                        ),
                        waiter_id="tool:" + stable_id,
                        requirements={"call_id": stable_id},
                    )
                    decision = {"approved": getattr(response, "approved", False)}
                else:
                    from langgraph.types import interrupt

                    decision = interrupt(
                        {
                            "type": "tool_approval",
                            "tool": name,
                            "call_id": stable_id,
                            "arguments_hash": digest,
                        }
                    )
            if not isinstance(decision, dict) or decision.get("approved") is not True:
                raise AppError(Code.TOOL_APPROVAL_DENIED, "工具操作未获批准", 403)
            async with self.resources.db.transaction(context.principal) as session:
                row = (
                    await session.execute(
                        select(ToolCall)
                        .where(ToolCall.run_id == context.run_id, ToolCall.call_id == stable_id)
                        .with_for_update()
                    )
                ).scalar_one_or_none()
                if row:
                    if row.arguments_hash != digest or row.tool != name:
                        raise AppError(Code.IDEMPOTENCY_CONFLICT, "调用 ID 已用于其他参数", 409)
                    if row.status == "succeeded":
                        return {"ok": True, "result": row.result}
                    raise AppError(Code.TOOL_OUTCOME_UNKNOWN, "先前副作用结果不确定，需要核对", 409)
                session.add(
                    ToolCall(
                        tenant_id=context.principal.tenant_id,
                        subject_id=context.principal.subject_id,
                        run_id=context.run_id,
                        call_id=stable_id,
                        tool=name,
                        arguments_hash=digest,
                        status="executing",
                    )
                )
        try:
            started = monotonic()
            async with self.limit, asyncio.timeout(self.config.timeout_seconds):
                with self.resources.telemetry.tracer.start_as_current_span(
                    "工具调用", attributes={"tool.name": name}
                ):
                    if inspect.iscoroutinefunction(definition.function):
                        result = await definition.function(**values)
                    else:
                        result = await asyncio.to_thread(definition.function, **values)
                        if inspect.isawaitable(result):
                            result = await result
                    json.dumps(result)
            if definition.write:
                async with self.resources.db.transaction(context.principal) as session:
                    row = (
                        await session.execute(
                            select(ToolCall)
                            .where(ToolCall.run_id == context.run_id, ToolCall.call_id == stable_id)
                            .with_for_update()
                        )
                    ).scalar_one()
                    row.status, row.result = "succeeded", result
            elapsed = monotonic() - started
            tool_calls_total.labels(name, "success").inc()
            tool_duration_seconds.labels(name).observe(elapsed)
            self.resources.logger.info("工具执行完成", tool=name, run_id=context.run_id)
            return {"ok": True, "result": result}
        except BaseException:
            tool_calls_total.labels(name, "error").inc()
            if definition.write:
                async with self.resources.db.transaction(context.principal) as session:
                    row = (
                        await session.execute(
                            select(ToolCall).where(
                                ToolCall.run_id == context.run_id, ToolCall.call_id == stable_id
                            )
                        )
                    ).scalar_one()
                    row.status = "unknown"
            raise

    def langchain(self, names: list[str]):
        """返回 LangChain 原生 StructuredTool，只绑定显式传入的工具。"""
        from langchain_core.tools import StructuredTool

        output = []
        for name in names:
            if name not in self.items:
                raise AppError(Code.TOOL_NOT_FOUND, "工具未注册", 404)
            definition = self.items[name]

            def factory(tool_name):
                """固定框架工具绑定名称，所有调用继续经过统一治理。"""

                async def invoke(**kwargs):
                    """把框架参数交给统一工具治理。"""
                    return await self.execute(tool_name, kwargs)

                return invoke

            output.append(
                StructuredTool.from_function(
                    name=name,
                    description=definition.description,
                    args_schema=definition.schema,
                    coroutine=factory(name),
                )
            )
        return output

    def llama(self, names: list[str]):
        """返回 LlamaIndex 原生 FunctionTool，复用相同权限和副作用规则。"""
        from llama_index.core.tools import FunctionTool

        output = []
        for name in names:
            definition = self.items[name]

            def factory(tool_name):
                """固定框架工具绑定名称，所有调用继续经过统一治理。"""

                async def invoke(**kwargs):
                    """把原生异步工具调用交给统一治理。"""
                    return await self.execute(tool_name, kwargs)

                return invoke

            output.append(
                FunctionTool.from_defaults(
                    async_fn=factory(name),
                    name=name,
                    description=definition.description,
                    fn_schema=definition.schema,
                )
            )
        return output
