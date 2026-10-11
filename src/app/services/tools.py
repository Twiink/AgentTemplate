"""真实常用工具与执行治理；注册并不自动授权给模型，绑定由项目显式完成。"""

from __future__ import annotations

import asyncio
import hashlib
import inspect
import ipaddress
import json
import os
import socket
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import aiohttp
from aiohttp.abc import AbstractResolver
from pydantic import BaseModel, Field
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import create_async_engine

from app.core.errors import AppError
from app.core.observability import tools
from app.db.models import ToolCall
from app.runtime.contracts import current_run


class ReadFile(BaseModel):
    """工作目录内 UTF-8 文件读取参数。"""

    path: str


class WriteFile(ReadFile):
    """写入必须给出稳定 operation_id，恢复时重复调用复用同一结果。"""

    content: str
    operation_id: str = Field(min_length=1, max_length=200)


class SearchFiles(BaseModel):
    """文件名与内容搜索参数；最多扫描固定数量文件。"""

    query: str = Field(min_length=1, max_length=500)
    limit: int = Field(default=20, ge=1, le=100)


class FetchURL(BaseModel):
    """仅支持受允许列表限制的 HTTP GET，不开放任意请求方法或身份头。"""

    url: str


class SearchWeb(BaseModel):
    """真实 Tavily 搜索参数。"""

    query: str = Field(min_length=1, max_length=1000)
    limit: int = Field(default=5, ge=1, le=10)


class ReadSQL(BaseModel):
    """查询使用参数绑定；数据库必须为专门只读角色。"""

    query: str = Field(min_length=1, max_length=20000)
    parameters: dict[str, Any] = Field(default_factory=dict)


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


class SafeResolver(AbstractResolver):
    """把验证后的 DNS 地址直接交给连接器，避免检查和连接之间再次解析目标。"""

    def __init__(self, allow_private: bool):
        self.allow_private = allow_private

    async def resolve(self, host, port=0, family=socket.AF_INET):
        """拒绝私网、回环、链路本地及保留地址；所有返回地址均经过检查。"""
        results = await asyncio.get_running_loop().getaddrinfo(
            host, port, family=family, type=socket.SOCK_STREAM
        )
        output = []
        for af, _, _, _, address in results:
            ip = ipaddress.ip_address(address[0])
            if not self.allow_private and not ip.is_global:
                raise AppError("http_target_denied", "目标地址不允许访问", 403)
            output.append(
                {
                    "hostname": host,
                    "host": address[0],
                    "port": port,
                    "family": af,
                    "proto": 0,
                    "flags": socket.AI_NUMERICHOST,
                }
            )
        return output

    async def close(self):
        """解析器无外部池资源。"""


class ToolRegistry:
    """统一参数校验、权限、确认、超时、并发、审计和副作用账本。"""

    def __init__(self, resources):
        self.resources = resources
        self.config = resources.settings.tools
        self.limit = asyncio.Semaphore(self.config.concurrency)
        self.items: dict[str, ToolDefinition] = {}
        self.register(
            ToolDefinition(
                "file_read", "读取工作目录内文本文件", ReadFile, self.read_file, "tools:files:read"
            )
        )
        self.register(
            ToolDefinition(
                "file_write",
                "写入工作目录内文本文件，需要确认",
                WriteFile,
                self.write_file,
                "tools:files:write",
                True,
            )
        )
        self.register(
            ToolDefinition(
                "file_search",
                "搜索工作目录内文件名及文本",
                SearchFiles,
                self.search_files,
                "tools:files:read",
            )
        )
        self.register(
            ToolDefinition(
                "http_fetch", "获取允许域名的网页正文", FetchURL, self.fetch, "tools:http"
            )
        )
        self.register(
            ToolDefinition(
                "web_search", "通过 Tavily 搜索公开网页", SearchWeb, self.web_search, "tools:search"
            )
        )
        self.register(
            ToolDefinition(
                "sql_read", "使用专用只读连接执行查询", ReadSQL, self.sql_read, "tools:sql"
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
            raise AppError("tool_not_found", "工具未注册", 404)
        definition = self.items[name]
        context = current_run.get()
        if not context:
            raise AppError("run_context_required", "工具必须在可信运行上下文中调用", 403)
        context.principal.require(definition.scope)
        values = definition.schema.model_validate(arguments).model_dump(
            exclude_unset=definition.omit_unset
        )
        digest = hashlib.sha256(
            json.dumps(values, sort_keys=True, default=str).encode()
        ).hexdigest()
        stable_id = call_id or values.get("operation_id") or digest
        if definition.write:
            if call_id is None and not values.get("operation_id"):
                raise AppError("operation_id_required", "写工具需要稳定 operation_id")
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
                raise AppError("tool_approval_denied", "工具操作未获批准", 403)
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
                        raise AppError("idempotency_conflict", "调用 ID 已用于其他参数", 409)
                    if row.status == "succeeded":
                        return {"ok": True, "result": row.result}
                    raise AppError("tool_outcome_unknown", "先前副作用结果不确定，需要核对", 409)
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
            tools.labels(name, "success").inc()
            self.resources.logger.info("工具执行完成", tool=name, run_id=context.run_id)
            return {"ok": True, "result": result}
        except BaseException:
            tools.labels(name, "error").inc()
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
                raise AppError("tool_not_found", "工具未注册", 404)
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

    def file_descriptor(self, path: str, *, write: bool = False):
        """逐级使用 dir_fd 和 O_NOFOLLOW 打开路径，防止符号链接及检查后替换攻击。"""
        parts = Path(path).parts
        if not parts or Path(path).is_absolute() or any(p in {"..", "."} for p in parts):
            raise AppError("path_denied", "文件路径越界", 403)
        root = self.config.directory.resolve()
        root.mkdir(parents=True, exist_ok=True)
        fd = os.open(root, os.O_RDONLY | os.O_DIRECTORY)
        try:
            for part in parts[:-1]:
                next_fd = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
                os.close(fd)
                fd = next_fd
            flags = os.O_NOFOLLOW | (
                os.O_WRONLY | os.O_CREAT | os.O_TRUNC if write else os.O_RDONLY
            )
            return os.open(parts[-1], flags, 0o600, dir_fd=fd)
        except OSError as error:
            raise AppError("file_access_denied", "文件不存在或路径不允许访问", 403) from error
        finally:
            os.close(fd)

    async def read_file(self, path: str):
        """限制文件大小并按 UTF-8 读取，二进制文件给出明确错误。"""

        def read():
            """在工作线程内读取有限字节，拒绝越界及超大文件。"""
            with os.fdopen(self.file_descriptor(path), "rb") as handle:
                data = handle.read(self.config.max_bytes + 1)
                if len(data) > self.config.max_bytes:
                    raise AppError("file_too_large", "文件超过工具读取限制")
                return data.decode("utf-8")

        return await asyncio.to_thread(read)

    async def write_file(self, path: str, content: str, operation_id: str):
        """执行经批准的文件写入；大小在打开文件之前校验。"""
        data = content.encode()
        if len(data) > self.config.max_bytes:
            raise AppError("file_too_large", "写入内容超过限制")

        def write():
            """写入已批准内容并同步文件，错误由调用账本记录。"""
            with os.fdopen(self.file_descriptor(path, write=True), "wb") as handle:
                handle.write(data)
                handle.flush()
                os.fsync(handle.fileno())

        await asyncio.to_thread(write)
        return {"path": path, "bytes": len(data)}

    async def search_files(self, query: str, limit: int):
        """扫描最多一千个普通文件；跳过链接及无法读取文件，结果不回显完整内容。"""
        root = self.config.directory.resolve()
        results = []
        for count, path in enumerate(root.rglob("*")):
            if count >= 1000 or len(results) >= limit:
                break
            if path.is_symlink() or not path.is_file():
                continue
            relative = str(path.relative_to(root))
            try:
                content = await self.read_file(relative)
            except (AppError, UnicodeError):
                continue
            if query in relative or query in content:
                results.append(
                    {
                        "path": relative,
                        "matching_lines": [
                            i + 1 for i, line in enumerate(content.splitlines()) if query in line
                        ][:20],
                    }
                )
        return results

    async def fetch(self, url: str):
        """逐跳检查允许域名及实际 IP；流式读取限制字节数，禁止 URL 自带凭据。"""
        resolver = SafeResolver(self.config.allow_private_networks)
        connector = aiohttp.TCPConnector(resolver=resolver, use_dns_cache=False)
        async with aiohttp.ClientSession(
            connector=connector,
            timeout=aiohttp.ClientTimeout(total=self.config.timeout_seconds),
            trust_env=False,
        ) as client:
            for _ in range(4):
                parsed = urlsplit(url)
                if (
                    parsed.scheme not in {"http", "https"}
                    or parsed.username
                    or parsed.password
                    or parsed.hostname not in self.config.allowed_hosts
                ):
                    raise AppError("http_target_denied", "目标不在允许列表", 403)
                try:
                    literal = ipaddress.ip_address(parsed.hostname or "")
                    if (
                        literal is not None
                        and not self.config.allow_private_networks
                        and not literal.is_global
                    ):
                        raise AppError("http_target_denied", "目标地址不允许访问", 403)
                except ValueError:
                    pass
                async with client.get(url, allow_redirects=False) as response:
                    if response.status in {301, 302, 303, 307, 308}:
                        from urllib.parse import urljoin

                        url = urljoin(url, response.headers.get("Location", ""))
                        continue
                    if response.status >= 400:
                        raise AppError("http_upstream_error", "目标返回错误", 502)
                    data = bytearray()
                    async for chunk in response.content.iter_chunked(8192):
                        data.extend(chunk)
                        if len(data) > self.config.max_bytes:
                            raise AppError("http_response_too_large", "响应超过限制")
                    return {
                        "url": url,
                        "status": response.status,
                        "content": data.decode("utf-8", errors="replace"),
                    }
        raise AppError("http_redirect_limit", "重定向次数超过限制")

    async def web_search(self, query: str, limit: int):
        """调用真实 Tavily HTTP API；未配置密钥时准确诊断。"""
        if not self.config.tavily_key:
            raise AppError("search_not_configured", "Tavily 未配置", 503)
        response = await self.resources.http[""].post(
            "https://api.tavily.com/search",
            headers={"Authorization": "Bearer " + self.config.tavily_key.get_secret_value()},
            json={"query": query, "max_results": limit, "include_raw_content": False},
        )
        if response.status_code >= 400:
            raise AppError(
                "search_upstream_error", "网页搜索调用失败", 502, response.status_code >= 500
            )
        return response.json()["results"]

    async def sql_read(self, query: str, parameters: dict[str, Any]):
        """通过只读事务与非特权只读角色执行单条查询；超时和返回数量均受限制。"""
        if not self.config.readonly_database_url:
            raise AppError("sql_not_configured", "专用只读连接未配置", 503)
        if ";" in query or not query.lstrip().lower().startswith(("select ", "with ")):
            raise AppError("sql_denied", "只接受单条查询", 403)
        import re

        dangerous = re.compile(
            r"(?i)\b(set_config|pg_sleep|dblink|lo_import|lo_export|pg_read_file|pg_write_file|pg_advisory|nextval|setval|pg_terminate_backend|pg_cancel_backend)\w*\s*\("
        )
        if dangerous.search(query):
            raise AppError("sql_denied", "查询包含受限函数", 403)
        import sqlglot
        from sqlglot import exp

        try:
            statements = sqlglot.parse(query, read="postgres")
            if len(statements) != 1 or not isinstance(statements[0], exp.Query):
                raise ValueError("仅查询")
            parsed = statements[0]
            ctes = {cte.alias.lower() for cte in parsed.find_all(exp.CTE)}
            for table in parsed.find_all(exp.Table):
                name = ".".join(
                    part for part in [table.catalog, table.db, table.name] if part
                ).lower()
                if name not in ctes and name not in {
                    v.lower() for v in self.config.sql_allowed_tables
                }:
                    raise AppError("sql_table_denied", "查询表不在只读允许列表", 403)
            functions = {
                "COUNT",
                "SUM",
                "AVG",
                "MIN",
                "MAX",
                "COALESCE",
                "NULLIF",
                "LOWER",
                "UPPER",
                "LENGTH",
                "CHAR_LENGTH",
                "ROUND",
                "ABS",
                "CAST",
                "TRY_CAST",
                "SUBSTRING",
                "DATE_TRUNC",
                "EXTRACT",
                "CURRENT_DATE",
                "CURRENT_TIMESTAMP",
                "NOW",
                "CONCAT",
                "TRIM",
                "ARRAY_AGG",
                "STRING_AGG",
                "JSON_EXTRACT",
                "JSON_EXTRACT_SCALAR",
            }
            for function in parsed.find_all(exp.Func):
                name = (
                    function.name.upper()
                    if isinstance(function, exp.Anonymous)
                    else function.sql_name()
                )
                if name not in functions:
                    raise AppError("sql_function_denied", "查询函数不在允许列表", 403)
        except AppError:
            raise
        except Exception as error:
            raise AppError("sql_denied", "查询无法安全解析", 403) from error
        context = current_run.get()
        if context is None:
            raise AppError("run_context_required", "SQL 查询需要可信运行身份", 403)
        engine = create_async_engine(self.config.readonly_database_url.get_secret_value())
        try:
            async with engine.connect() as connection, connection.begin():
                await connection.execute(text("SET TRANSACTION READ ONLY"))
                await connection.execute(text("SET LOCAL statement_timeout='5s'"))
                await connection.execute(
                    text(
                        "SELECT set_config('app.tenant_id',:tenant,true), "
                        "set_config('app.subject_id',:subject,true), set_config('app.system','false',true)"
                    ),
                    {
                        "tenant": context.principal.tenant_id,
                        "subject": context.principal.subject_id,
                    },
                )
                role = (
                    await connection.execute(
                        text(
                            "SELECT rolsuper, rolbypassrls FROM pg_roles WHERE rolname=current_user"
                        )
                    )
                ).one()
                if role.rolsuper or role.rolbypassrls:
                    raise AppError("sql_role_denied", "只读工具不允许特权数据库角色", 403)
                result = await connection.stream(text(query), parameters)
                rows = []
                async for row in result.mappings():
                    rows.append(dict(row))
                    if len(rows) >= self.config.sql_row_limit:
                        break
                await result.close()
                return json.loads(json.dumps(rows, default=str))
        finally:
            await engine.dispose()
