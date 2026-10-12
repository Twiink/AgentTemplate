"""受允许列表限制的 HTTP GET；逐跳检查域名与实际连接 IP，限制响应大小并截断正文。"""

from __future__ import annotations

import asyncio
import ipaddress
import socket
from urllib.parse import urlsplit

import aiohttp
from aiohttp.abc import AbstractResolver
from pydantic import BaseModel

from app.core.config import ToolConfig
from app.core.errors import AppError


class FetchURL(BaseModel):
    """仅支持受允许列表限制的 HTTP GET，不开放任意请求方法或身份头。"""

    url: str


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


async def fetch(config: ToolConfig, url: str):
    """逐跳检查允许域名及实际 IP；流式读取限制字节数，禁止 URL 自带凭据。"""
    resolver = SafeResolver(config.allow_private_networks)
    connector = aiohttp.TCPConnector(resolver=resolver, use_dns_cache=False)
    async with aiohttp.ClientSession(
        connector=connector,
        timeout=aiohttp.ClientTimeout(total=config.timeout_seconds),
        trust_env=False,
    ) as client:
        for _ in range(4):
            parsed = urlsplit(url)
            if (
                parsed.scheme not in {"http", "https"}
                or parsed.username
                or parsed.password
                or parsed.hostname not in config.allowed_hosts
            ):
                raise AppError("http_target_denied", "目标不在允许列表", 403)
            try:
                literal = ipaddress.ip_address(parsed.hostname or "")
                if (
                    literal is not None
                    and not config.allow_private_networks
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
                    if len(data) > config.max_bytes:
                        raise AppError("http_response_too_large", "响应超过限制")
                content = data.decode("utf-8", errors="replace")
                limit = config.fetch_max_chars
                # 截断后显式标记，模型和调用方都能知道内容不完整。
                return {
                    "url": url,
                    "status": response.status,
                    "content": content[:limit],
                    "truncated": len(content) > limit,
                    "total_chars": len(content),
                }
    raise AppError("http_redirect_limit", "重定向次数超过限制")
