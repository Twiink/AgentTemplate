"""验证模板公开配置和 Agent/工具扩展契约。"""

from __future__ import annotations

import ast
import asyncio
import json
import re
from contextlib import nullcontext
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml
from pydantic import BaseModel, Field

from app.core.config import Settings, load_settings
from app.core.errors import AppError, Code
from app.core.quota import QuotaManager
from app.core.security import Principal
from app.runtime.contracts import RunContext, current_run
from app.tools.registry import ToolDefinition, ToolRegistry

ROOT = Path(__file__).resolve().parents[1]


def test_readme_primary_configuration_matches_settings_schema():
    """验证 README 主配置片段符合运行时 Settings schema。"""
    readme = (ROOT / "README.md").read_text()
    match = re.search(r"```yaml\n# config/base\.yaml\n(.*?)```", readme, re.DOTALL)
    assert match is not None
    settings = Settings.model_validate(yaml.safe_load(match.group(1)))
    assert settings.modules == ["storage"]
    assert settings.mcp["filesystem"].transport == "stdio"
    assert settings.tools.allowed_hosts == ["example.com", "api.openai.com"]


def test_empty_configuration_defaults_to_minimal_preset():
    """验证 CLI 和 Settings 共用的默认预设为 minimal。"""
    settings = Settings.model_validate({})
    assert settings.preset == "minimal"
    assert settings.modules == ["storage"]


def test_legacy_run_budget_preserves_monthly_limit_unless_explicitly_disabled():
    """验证旧 run_cost_budget 配置不会因升级而失去主体月度限制。"""
    legacy = load_settings(overrides={"run_cost_budget": 2.5})
    disabled = load_settings(overrides={"run_cost_budget": 2.5, "monthly_cost_budget": None})
    assert legacy.run_cost_budget == 2.5
    assert legacy.monthly_cost_budget == 2.5
    assert disabled.monthly_cost_budget is None


def test_minimal_pruning_preserves_optional_observability_files():
    """验证轻量档裁剪不会删除共享观测部署配置。"""
    from app.cli.project import FULL_ONLY_PATHS

    assert "compose.full.yaml" in FULL_ONLY_PATHS
    assert "deploy/kubernetes" in FULL_ONLY_PATHS
    assert "compose.observability.yaml" not in FULL_ONLY_PATHS
    assert "deploy/observability" not in FULL_ONLY_PATHS


def test_scaffold_agent_creates_parseable_files_and_refuses_overwrite(tmp_path):
    """验证 Agent 脚手架生成可解析源码、Prompt 并拒绝覆盖。"""
    from app.cli.scaffold import agent_files, create_files

    files = agent_files(tmp_path, "researcher", "openai-main")
    create_files(files)
    ast.parse((tmp_path / "src/app/agents/researcher/agent.py").read_text())
    prompt = (tmp_path / "prompts/researcher/system.md").read_text()
    assert "purpose: researcher Agent" in prompt
    with pytest.raises(AppError) as raised:
        create_files(files)
    assert raised.value.status == 409
    from app.cli.scaffold import create_files as create_tool_files
    from app.cli.scaffold import tool_file, tool_source

    tool_path = tool_file(tmp_path, "researcher", "summarize")
    create_tool_files(
        {
            tool_path.parent / "__init__.py": '"""Agent 专用工具。"""\n',
            tool_path: tool_source("researcher", "summarize"),
        }
    )
    ast.parse(tool_path.read_text())


def test_scaffold_identifier_rejects_path_traversal():
    """验证脚手架名称不接受路径穿越和源码注入字符。"""
    from typer import BadParameter

    from app.cli.scaffold import validate_identifier

    with pytest.raises(BadParameter):
        validate_identifier("../escape", "Agent 名称")


def test_project_initialization_derives_safe_project_slug_and_peer(tmp_path):
    """验证项目名称可安全作为标识，派生仓库会替换模板默认 peer。"""
    from app.cli.project import project_slug, refresh_project_peer

    assert project_slug("My Assistant") == "my-assistant"
    assert project_slug("中文助手").startswith("agent-")
    peer_config = tmp_path / ".openviking/config.json"
    peer_config.parent.mkdir()
    peer_config.write_text(
        '{"version":1,"peer":{"id":"github.com-twiink-agenttemplate"},'
        '"recall":{"peer_scope":"actor"}}',
        encoding="utf-8",
    )
    assert refresh_project_peer(peer_config, "https://github.com/example/my-assistant.git")
    result = yaml.safe_load(peer_config.read_text(encoding="utf-8"))
    assert result["peer"]["id"].startswith("project-")
    assert result["recall"]["peer_scope"] == "actor"
    assert not refresh_project_peer(peer_config, "https://github.com/example/my-assistant.git")


def test_documented_production_configurations_match_settings_schema():
    """验证公开的生产配置片段符合运行时 Settings schema。"""
    for path in (ROOT / "README.md", ROOT / "docs/配置说明.md"):
        text = path.read_text()
        match = re.search(r"```yaml\n# config/production\.yaml\n(.*?)```", text, re.DOTALL)
        assert match is not None
        settings = Settings.model_validate(yaml.safe_load(match.group(1)))
        assert settings.tracing.enabled or settings.sentry.enabled


def test_readme_agent_example_is_valid_python():
    """验证 README 的首个 Agent 示例可被 Python 解析。"""
    readme = (ROOT / "README.md").read_text()
    match = re.search(
        r"```python\n# src/app/agents/assistant/agent\.py\n(.*?)```", readme, re.DOTALL
    )
    assert match is not None
    ast.parse(match.group(1))


def test_agent_custom_tool_uses_the_shared_governance_executor():
    """验证 Agent 专属工具也经过统一权限与执行包装。"""

    class NormalizeInput(BaseModel):
        """文本规范化参数。"""

        text: str = Field(description="Text to trim")

    def normalize_text(text: str) -> str:
        """去除文本首尾空白。"""
        return text.strip()

    class Logger:
        """忽略测试期间的结构化日志。"""

        def info(self, *args, **kwargs):
            """接受日志调用但不产生输出。"""
            return None

    tracer = SimpleNamespace(start_as_current_span=lambda *args, **kwargs: nullcontext())
    resources = SimpleNamespace(
        settings=SimpleNamespace(tools=SimpleNamespace(timeout_seconds=1)),
        telemetry=SimpleNamespace(tracer=tracer),
        logger=Logger(),
    )
    registry = ToolRegistry.__new__(ToolRegistry)
    registry.resources = resources
    registry.config = resources.settings.tools
    registry.limit = __import__("asyncio").Semaphore(1)
    registry.items = {}

    custom = ToolDefinition(
        name="normalize_text",
        description="Trim text",
        schema=NormalizeInput,
        function=normalize_text,
        scope="tools:custom:normalize",
    )
    tool = registry.langchain([], custom=[custom])[0]
    execute_definition = registry.execute_definition
    call_ids = []

    async def record_call_id(definition, arguments, *, call_id=None):
        """收集框架注入的工具调用 ID 后继续执行统一治理。"""
        call_ids.append(call_id)
        return await execute_definition(definition, arguments, call_id=call_id)

    registry.execute_definition = record_call_id
    context = RunContext(
        principal=Principal("tenant", "subject", frozenset({"tools:custom:normalize"})),
        run_id="run-1",
        thread_id="thread-1",
        lease_token="lease-1",
        resources=resources,
        deadline=1e30,
        token_budget=100,
        cost_budget=None,
    )
    token = current_run.set(context)
    try:
        result = asyncio.run(
            tool.ainvoke(
                {
                    "type": "tool_call",
                    "name": "normalize_text",
                    "args": {"text": "  text  "},
                    "id": "call-123",
                }
            )
        )
    finally:
        current_run.reset(token)
    assert json.loads(result.content) == {"ok": True, "result": "text"}
    assert call_ids == ["call-123"]


def test_time_builtin_adapts_registry_arguments_to_schema_object():
    """验证内置时间工具可从注册表按关键字参数调用。"""

    class Logger:
        """忽略测试期间的结构化日志。"""

        def info(self, *args, **kwargs):
            """接受日志调用但不产生输出。"""
            return None

    tracer = SimpleNamespace(start_as_current_span=lambda *args, **kwargs: nullcontext())
    settings = Settings.model_validate({"modules": []})
    resources = SimpleNamespace(
        settings=settings,
        http={"": object()},
        telemetry=SimpleNamespace(tracer=tracer),
        logger=Logger(),
    )
    registry = ToolRegistry(resources)
    context = RunContext(
        principal=Principal("tenant", "subject", frozenset({"tools:time"})),
        run_id="run-time",
        thread_id="thread-time",
        lease_token="lease-time",
        resources=resources,
        deadline=1e30,
        token_budget=100,
        cost_budget=None,
    )
    tool = registry.langchain(["current_time"])[0]
    token = current_run.set(context)
    try:
        result = asyncio.run(
            tool.ainvoke(
                {
                    "type": "tool_call",
                    "name": "current_time",
                    "args": {"timezone": "UTC"},
                    "id": "time-call-1",
                }
            )
        )
    finally:
        current_run.reset(token)
    payload = json.loads(result.content)
    assert payload["ok"] is True
    assert payload["result"]["timezone"] == "UTC"


def test_monthly_quota_uses_its_own_limit_setting():
    """验证月度配额不复用单次运行费用预算。"""

    class FakeRedis:
        """返回预设的主体月度累计值。"""

        async def hget(self, key, field):
            """返回固定配额使用金额。"""
            value = 4.0 if field == "total" else None
            return str(value) if value is not None else None

        async def get(self, key):
            """模拟不存在的旧版字符串配额值。"""
            return None

        async def hsetnx(self, key, field, value):
            """仅在新哈希缺少字段时迁入旧版累计金额。"""
            return 0

        async def expire(self, key, seconds):
            """模拟刷新配额哈希有效期。"""
            return True

    principal = Principal("tenant", "subject", frozenset())
    settings = SimpleNamespace(monthly_cost_budget=5.0, run_cost_budget=0.01)
    manager = QuotaManager(
        FakeRedis(), SimpleNamespace(warning=lambda *args, **kwargs: None), settings
    )
    asyncio.run(manager.check(principal))

    settings.monthly_cost_budget = 3.0
    try:
        asyncio.run(manager.check(principal))
    except AppError as error:
        assert error.code == Code.QUOTA_EXCEEDED
    else:
        raise AssertionError("monthly quota should reject at its independent limit")


def test_monthly_quota_recording_is_idempotent_per_run():
    """验证同一终态运行重复结算不会重复增加主体月度费用。"""

    class FakeRedis:
        """用字典模拟配额哈希和 Lua 原子结算。"""

        def __init__(self):
            self.records = {}

        async def eval(self, script, key_count, key, cost, settlement):
            """只对首次出现的 run settlement 累加金额。"""
            record = self.records.setdefault(key, {"total": 0.0, "runs": set()})
            if settlement not in record["runs"]:
                record["runs"].add(settlement)
                record["total"] += float(cost)
            return record["total"]

        async def hget(self, key, field):
            """读取配额哈希中的累计值。"""
            value = self.records.get(key, {}).get(field)
            return str(value) if value is not None else None

        async def get(self, key):
            """模拟旧版字符串配额键缺失。"""
            return None

        async def hsetnx(self, key, field, value):
            """仅在哈希缺少字段时写入一次。"""
            record = self.records.setdefault(key, {"total": 0.0, "runs": set()})
            if field in record:
                return 0
            record[field] = float(value)
            return 1

        async def expire(self, key, seconds):
            """模拟配额哈希过期设置。"""
            return True

    redis = FakeRedis()
    settings = SimpleNamespace(monthly_cost_budget=5.0)
    manager = QuotaManager(redis, SimpleNamespace(warning=lambda *args, **kwargs: None), settings)
    principal = Principal("tenant", "subject", frozenset())

    async def settle():
        """模拟数据库事务提交重试同一终态钩子。"""
        await manager.record(principal, 1.25, run_id="run-42")
        await manager.record(principal, 1.25, run_id="run-42")

    asyncio.run(settle())
    assert asyncio.run(manager.usage(principal))["used"] == 1.25


def test_monthly_quota_migrates_existing_string_counter():
    """验证旧版 Redis 字符串计数升级时保留本月已使用额度。"""

    class FakeRedis:
        """提供旧版字符串键和新版哈希键的最小替身。"""

        def __init__(self):
            self.hashes = {}
            self.strings = {}

        async def get(self, key):
            """返回模拟的旧版字符串值。"""
            return self.strings.get(key)

        async def hget(self, key, field):
            """读取新版哈希字段。"""
            value = self.hashes.get(key, {}).get(field)
            return str(value) if value is not None else None

        async def hsetnx(self, key, field, value):
            """将旧值复制到空的新哈希字段。"""
            target = self.hashes.setdefault(key, {})
            if field in target:
                return 0
            target[field] = value
            return 1

        async def expire(self, key, seconds):
            """接受配额哈希 TTL 设置。"""
            return True

    redis = FakeRedis()
    settings = SimpleNamespace(monthly_cost_budget=20.0)
    manager = QuotaManager(redis, SimpleNamespace(warning=lambda *args, **kwargs: None), settings)
    principal = Principal("tenant", "subject", frozenset())
    redis.strings[manager._legacy_key(principal)] = "7.5"

    usage = asyncio.run(manager.usage(principal))
    assert usage["used"] == 7.5
    assert redis.hashes[manager._key(principal)]["total"] == "7.5"
