"""网页搜索：调用 Tavily HTTP API；未配置密钥时返回明确的未配置错误。"""

from __future__ import annotations

import httpx
from pydantic import BaseModel, Field

from app.core.config import ToolConfig
from app.core.errors import AppError, Code


class SearchWeb(BaseModel):
    """真实 Tavily 搜索参数。"""

    query: str = Field(min_length=1, max_length=1000)
    limit: int = Field(default=5, ge=1, le=10)


async def web_search(config: ToolConfig, http: httpx.AsyncClient, query: str, limit: int):
    """调用真实 Tavily HTTP API；未配置密钥时准确诊断。"""
    if not config.tavily_key:
        raise AppError(Code.SEARCH_NOT_CONFIGURED, "Tavily 未配置")
    response = await http.post(
        "https://api.tavily.com/search",
        headers={"Authorization": "Bearer " + config.tavily_key.get_secret_value()},
        json={"query": query, "max_results": limit, "include_raw_content": False},
    )
    if response.status_code >= 400:
        raise AppError(
            "search_upstream_error", "网页搜索调用失败", 502, response.status_code >= 500
        )
    return response.json()["results"]
