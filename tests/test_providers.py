"""真实收费模型验收；仅在显式环境开关和有效配置存在时执行，缺省明确跳过。"""

import os

import pytest

from app.core.config import load_settings
from app.core.resources import Resources

pytestmark = pytest.mark.providers


@pytest.mark.asyncio
async def test_real_configured_providers():
    """用操作者授权的短输入验证服务商真实响应与用量，不生成业务 Agent。"""
    if os.getenv("AGENT_PROVIDER_ACCEPTANCE") != "1":
        pytest.skip("真实服务商调用未授权启用")
    settings = load_settings()
    if not settings.chat_models:
        pytest.skip("未配置真实聊天模型")
    from langchain_core.messages import HumanMessage
    from llama_index.core.llms import ChatMessage

    async with Resources(settings) as resources:
        for name in settings.chat_models:
            lc = await resources.models.chat(name).ainvoke([HumanMessage(content="回复确认两个字")])
            assert lc.content
            li = await resources.models.llama_chat(name).achat(
                [ChatMessage(role="user", content="回复确认两个字")]
            )
            assert li.message.content


@pytest.mark.asyncio
async def test_real_configured_embeddings():
    """真实向量模型必须返回声明的维度，未配置准确跳过。"""
    if os.getenv("AGENT_PROVIDER_ACCEPTANCE") != "1":
        pytest.skip("真实服务商调用未授权启用")
    settings = load_settings()
    if not settings.embedding_models:
        pytest.skip("未配置真实向量模型")
    async with Resources(settings) as resources:
        for name, profile in settings.embedding_models.items():
            vectors = await resources.models.embed(name, ["向量维度验收"])
            assert len(vectors[0]) == profile.dimensions
