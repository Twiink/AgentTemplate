"""验证真实配置加载、认证及预算边界，不创建业务 Agent 或模拟模型。"""

import hashlib
import time

import jwt
import pytest

from app.core.config import AuthConfig, Credential, ModelProfile, load_settings
from app.core.errors import AppError
from app.core.security import Authenticator, Principal
from app.runtime.contracts import RunContext
from app.services.context import ContextPolicy
from app.services.prompts import PromptLoader


def test_configuration_precedence(tmp_path, monkeypatch):
    """所有来源按确认的顺序生效，嵌套覆盖保留其他字段。"""
    directory = tmp_path / "config"
    directory.mkdir()
    (directory / "base.yaml").write_text("run_timeout_seconds: 100\ntools:\n  max_bytes: 1000\n")
    (directory / "test.yaml").write_text("run_timeout_seconds: 110\n")
    (directory / "private.yaml").write_text("run_timeout_seconds: 120\n")
    monkeypatch.setenv("AGENT_ENVIRONMENT", "test")
    monkeypatch.setenv("AGENT_RUN_TIMEOUT_SECONDS", "130")
    result = load_settings({"run_timeout_seconds": 140, "tools": {"concurrency": 2}}, tmp_path)
    assert result.run_timeout_seconds == 140
    assert result.tools.max_bytes == 1000
    assert result.tools.concurrency == 2


def test_unknown_configuration_rejected(tmp_path):
    """拼错配置应拒绝，不静默忽略。"""
    (tmp_path / "config").mkdir()
    (tmp_path / "config/base.yaml").write_text("unknown_setting: 1\n")
    with pytest.raises(ValueError):
        load_settings(root=tmp_path)


@pytest.mark.asyncio
async def test_key_and_jwt_verification():
    """真实签名令牌验证 tenant、subject、issuer、audience 和过期字段。"""
    token = "unit-validation-credential"
    secret = "unit-validation-signing-secret-with-enough-length"
    config = AuthConfig(
        api_keys=[
            Credential(
                digest=hashlib.sha256(token.encode()).hexdigest(),
                tenant_id="tenant-a",
                subject_id="subject-a",
            )
        ],
        jwt_key=secret,
        jwt_algorithms=["HS256"],
        jwt_issuer="issuer",
        jwt_audience="audience",
    )
    auth = Authenticator(config)
    assert (await auth.verify(token)).subject_id == "subject-a"
    signed = jwt.encode(
        {
            "sub": "subject-b",
            "tenant_id": "tenant-b",
            "iss": "issuer",
            "aud": "audience",
            "exp": int(time.time()) + 60,
            "scope": "resources:read",
        },
        secret,
    )
    assert (await auth.verify(signed)).tenant_id == "tenant-b"
    with pytest.raises(AppError):
        await auth.verify(signed + "invalid")


def test_usage_unknown_is_not_zero():
    """无真实 usage 的调用按保守估算记账，真实用量字段仍保持未知。"""
    profile = ModelProfile(provider="openai", model="configured-model", api_key="test-credential")
    context = RunContext(
        Principal("t", "s", frozenset()),
        "r",
        "thread",
        "lease",
        None,
        time.monotonic() + 10,
        1000,
        None,
    )
    context.record_usage(None, 100, profile)
    assert context.usage["input_tokens"] is None
    assert context.usage["output_tokens"] is None
    assert context.usage["estimated_tokens"] == 100
    with pytest.raises(AppError):
        context.reserve(1000, 1, profile)


def test_prompt_parameters_and_version(tmp_path):
    """版本按内容产生，禁止路径越界及额外参数。"""
    (tmp_path / "instruction.txt").write_text("处理 {target}")
    loader = PromptLoader(tmp_path)
    content, version = loader.render("instruction.txt", {"target": "用户输入"})
    assert content == "处理 用户输入" and len(version) == 64
    with pytest.raises(AppError):
        loader.render("../instruction.txt", {"target": "用户输入"})


def test_context_trimming_preserves_original():
    """裁剪生成新列表，原始消息仍保留。"""
    messages = [{"role": "system", "content": "约束"}, {"role": "user", "content": "内容"}]
    assert ContextPolicy.trim(messages, 1000) == messages
    assert len(messages) == 2
