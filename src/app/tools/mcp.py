"""MCP 连接在专门任务内创建及关闭；调用通过队列传递，避免跨任务退出取消作用域。"""

from __future__ import annotations

import asyncio
import os
from contextlib import AsyncExitStack
from typing import Any

from pydantic import Field, create_model

from app.core.errors import AppError
from app.tools.registry import ToolDefinition


class MCPConnection:
    """会话调用代理；不会把原生异步退出栈暴露给请求任务。"""

    def __init__(self, queue, task):
        self.queue, self.task = queue, task

    async def call_tool(self, name, arguments):
        """提交一次真实协议调用并等待结果；连接失败不会伪装为空工具结果。"""
        if self.task.done():
            raise AppError("mcp_disconnected", "MCP 连接已关闭", 503)
        result = asyncio.get_running_loop().create_future()
        await self.queue.put((name, arguments, result))
        return await result


class MCPManager:
    """按服务名管理连接任务、发现和工具注册，所有退出栈在所属任务内关闭。"""

    def __init__(self, resources):
        self.resources = resources
        self.connections = {}
        self.lock = asyncio.Lock()

    async def connect(self, name: str):
        """惰性启动真实连接；发现完成才返回，参数和秘密引用缺失时准确诊断。"""
        if name not in self.resources.settings.mcp:
            raise AppError("mcp_not_configured", "MCP 服务未配置", 503)
        async with self.lock:
            if name in self.connections:
                return self.connections[name][0]
            ready = asyncio.get_running_loop().create_future()
            queue = asyncio.Queue()
            task = asyncio.create_task(self.serve(name, queue, ready))
            connection = MCPConnection(queue, task)
            self.connections[name] = (connection, task)
            try:
                await ready
            except BaseException:
                self.connections.pop(name, None)
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
                raise
            return connection

    def resolve(self, refs):
        """解析秘密环境变量引用，永不把值写入日志或发现结果。"""
        values = {}
        for key, ref in refs.items():
            if ref not in os.environ:
                raise AppError("mcp_credential_missing", "MCP 凭据引用不存在", 503)
            values[key] = os.environ[ref]
        return values

    async def serve(self, name, queue, ready):
        """连接、发现、调用和退出在同一任务中执行，符合 SDK 的取消作用域约束。"""
        config = self.resources.settings.mcp[name]
        try:
            async with AsyncExitStack() as stack:
                from mcp import ClientSession, StdioServerParameters

                if config.transport == "stdio":
                    if not config.command:
                        raise AppError("mcp_configuration", "stdio command 未配置", 503)
                    from mcp.client.stdio import stdio_client

                    read, write = await stack.enter_async_context(
                        stdio_client(
                            StdioServerParameters(
                                command=config.command,
                                args=config.args,
                                env=self.resolve(config.environment_refs),
                            )
                        )
                    )
                else:
                    if not config.url:
                        raise AppError("mcp_configuration", "HTTP URL 未配置", 503)
                    import httpx2
                    from mcp.client.streamable_http import streamable_http_client

                    client = await stack.enter_async_context(
                        httpx2.AsyncClient(
                            headers=self.resolve(config.header_refs), timeout=30, trust_env=False
                        )
                    )
                    streams = await stack.enter_async_context(
                        streamable_http_client(config.url, http_client=client)
                    )
                    read, write = streams[:2]
                session = await stack.enter_async_context(ClientSession(read, write))
                async with asyncio.timeout(self.resources.settings.tools.timeout_seconds):
                    await session.initialize()
                    discovered = []
                    cursor = None
                    while True:
                        from mcp.types import PaginatedRequestParams

                        page = await session.list_tools(
                            params=PaginatedRequestParams(cursor=cursor)
                        )
                        discovered.extend(page.tools)
                        cursor = page.next_cursor
                        if cursor is None:
                            break
                # readOnlyHint 不明确时按写操作治理，项目仅通过 allowed_tools 决定可绑定范围。
                for tool in discovered:
                    if tool.name not in config.allowed_tools:
                        continue
                    write_tool = tool.name in config.write_tools or not (
                        tool.annotations and tool.annotations.read_only_hint is True
                    )
                    original_schema = dict(tool.input_schema)
                    schema = dict(original_schema)
                    if write_tool:
                        schema = {
                            **schema,
                            "properties": {
                                **schema.get("properties", {}),
                                "operation_id": {"type": "string", "minLength": 1},
                            },
                            "required": [*schema.get("required", []), "operation_id"],
                        }
                    model = self.schema(schema, f"MCP_{name}_{tool.name}")

                    def factory(remote_name, json_schema):
                        """固定原生名称与 JSON Schema；闭包不捕获循环中的最终值。"""

                        async def call(**arguments):
                            """校验完整协议约束后通过专门任务调用工具。"""
                            import jsonschema

                            arguments.pop("operation_id", None)
                            jsonschema.validate(arguments, json_schema)
                            connection = self.connections[name][0]
                            response = await connection.call_tool(remote_name, arguments)
                            if response.is_error:
                                raise AppError("mcp_tool_error", "MCP 工具返回错误", 502)
                            return response.model_dump(mode="json")

                        return call

                    self.resources.tools.register(
                        ToolDefinition(
                            f"mcp_{name}_{tool.name}",
                            tool.description or "MCP 工具",
                            model,
                            factory(tool.name, original_schema),
                            f"tools:mcp:{name}",
                            write_tool,
                            omit_unset=True,
                        )
                    )
                ready.set_result(True)
                while True:
                    request = await queue.get()
                    if request is None:
                        return
                    remote_name, arguments, result = request
                    if result.cancelled():
                        continue
                    try:
                        async with asyncio.timeout(self.resources.settings.tools.timeout_seconds):
                            response = await session.call_tool(remote_name, arguments)
                        if not result.done():
                            result.set_result(response)
                    except Exception as error:
                        if not result.done():
                            result.set_exception(error)
        except BaseException as error:
            if not ready.done():
                ready.set_exception(error)
            while not queue.empty():
                request = queue.get_nowait()
                if request and not request[2].done():
                    request[2].set_exception(AppError("mcp_disconnected", "MCP 连接已关闭", 503))
            if isinstance(error, asyncio.CancelledError):
                raise

    def schema(self, schema, name):
        """生成框架可识别的 Pydantic 参数模型，完整 JSON Schema 在实际执行前再验证。"""
        types = {
            "string": str,
            "integer": int,
            "number": float,
            "boolean": bool,
            "object": dict,
            "array": list,
        }
        fields: dict[str, Any] = {}
        for key, value in schema.get("properties", {}).items():
            fields[key] = (
                types.get(value.get("type"), Any),
                Field()
                if key in schema.get("required", [])
                else Field(default=value.get("default")),
            )
        return create_model(name, **fields)

    async def close(self):
        """发送退出指令并等待专门任务完成；超时任务取消，退出仍发生在任务内部。"""
        for connection, task in reversed(list(self.connections.values())):
            await connection.queue.put(None)
            try:
                await asyncio.wait_for(asyncio.shield(task), timeout=10)
            except TimeoutError:
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
        self.connections.clear()
