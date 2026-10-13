"""受允许列表限制的 HTTP GET；逐跳检查域名与实际连接 IP，限制响应大小并截断正文。

使用 trafilatura 提取网页正文并转为 Markdown，保留原始 HTML 作为备用。
"""

from __future__ import annotations

import asyncio
import ipaddress
import socket
from urllib.parse import urlsplit

import aiohttp
from aiohttp.abc import AbstractResolver
from pydantic import BaseModel, Field

from app.core.config import ToolConfig
from app.core.errors import AppError, Code


class FetchURL(BaseModel):
    """仅支持受允许列表限制的 HTTP GET，不开放任意请求方法或身份头。"""

    url: str
    extract_content: bool = Field(
        default=True, description="是否提取正文并转为 Markdown（默认 true）"
    )


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
                raise AppError(Code.HTTP_TARGET_DENIED, "目标地址不允许访问", 403)
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


async def fetch(config: ToolConfig, url: str, extract_content: bool = True):
    """逐跳检查允许域名及实际 IP；流式读取限制字节数，禁止 URL 自带凭据。

    当 extract_content 为 True 时，使用 trafilatura 提取正文并转为 Markdown。
    """
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
                raise AppError(Code.HTTP_TARGET_DENIED, "目标不在允许列表", 403)
            try:
                literal = ipaddress.ip_address(parsed.hostname or "")
                if (
                    literal is not None
                    and not config.allow_private_networks
                    and not literal.is_global
                ):
                    raise AppError(Code.HTTP_TARGET_DENIED, "目标地址不允许访问", 403)
            except ValueError:
                pass
            async with client.get(url, allow_redirects=False) as response:
                if response.status in {301, 302, 303, 307, 308}:
                    from urllib.parse import urljoin

                    url = urljoin(url, response.headers.get("Location", ""))
                    continue
                if response.status >= 400:
                    raise AppError(Code.HTTP_UPSTREAM_ERROR, "目标返回错误", 502)
                # 读取响应
                data = bytearray()
                async for chunk in response.content.iter_chunked(8192):
                    data.extend(chunk)
                    if len(data) > config.max_bytes:
                        raise AppError(Code.HTTP_RESPONSE_TOO_LARGE, "响应超过限制")

                html_content = data.decode("utf-8", errors="replace")

                # 如果需要提取正文
                if extract_content:
                    try:
                        import trafilatura

                        # 提取正文并转为 Markdown
                        extracted = trafilatura.extract(
                            html_content,
                            output_format="markdown",
                            include_tables=True,
                            include_links=True,
                            include_images=False,  # 图片链接保留但不嵌入
                        )

                        if extracted:
                            content = extracted
                            extraction_success = True
                        else:
                            # 提取失败，回退到原始 HTML
                            content = html_content
                            extraction_success = False
                    except ImportError:
                        # trafilatura 未安装，回退到原始 HTML
                        content = html_content
                        extraction_success = False
                    except Exception:
                        # 提取出错，回退到原始 HTML
                        content = html_content
                        extraction_success = False
                else:
                    content = html_content
                    extraction_success = False

                limit = config.fetch_max_chars
                truncated = len(content) > limit

                result = {
                    "url": url,
                    "status": response.status,
                    "content": content[:limit],
                    "truncated": truncated,
                    "total_chars": len(content),
                }

                # 如果启用了提取，添加提取状态
                if extract_content:
                    result["content_extracted"] = extraction_success
                    if extraction_success:
                        result["format"] = "markdown"
                    else:
                        result["format"] = "html"

                return result

    raise AppError(Code.HTTP_REDIRECT_LIMIT, "重定向次数超过限制")
