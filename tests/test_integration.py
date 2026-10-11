"""真实 PostgreSQL、Redis、原生检查点与 API 验收；依赖不可用时明确跳过。"""

import asyncio
import hashlib
import os
from typing import TypedDict
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from pydantic import BaseModel
from sqlalchemy import select

from app.core.config import load_settings
from app.core.errors import AppError
from app.core.resources import Resources
from app.core.security import Principal
from app.db.models import Run, Thread
from app.main import create_app
from app.runtime.contracts import AgentRegistration

pytestmark = pytest.mark.integration


@pytest.fixture
def runtime_settings():
    """显式外部测试环境优先，否则使用本机私有开发配置，不清空数据库。"""
    database = os.getenv("AGENT_TEST_DATABASE_URL")
    redis = os.getenv("AGENT_TEST_REDIS_URL")
    if database:
        return load_settings(
            {
                "environment": "test",
                "database_url": database,
                "redis_url": redis or "redis://localhost:6379/0",
            }
        )
    if not os.path.exists("config/private.yaml"):
        pytest.skip("未配置真实集成数据库")
    return load_settings({"environment": "test"})


class GraphInput(BaseModel):
    """原生运行适配验收的状态 Schema，只有测试期间注册。"""

    value: int


class GraphState(TypedDict):
    """验收原生状态保存及恢复，不涉及模型或业务工具。"""

    value: int


def graph_factory(context, checkpointer):
    """构建真实 StateGraph，通过原生 interrupt 验证人工恢复。"""
    from langgraph.graph import END, START, StateGraph
    from langgraph.types import interrupt

    def approval(state):
        """暂停并消费实际人工响应，最终状态用于验收检查点。"""
        response = interrupt({"type": "approval", "value": state["value"]})
        return {"value": state["value"] + response["increment"]}

    graph = StateGraph(GraphState)
    graph.add_node("approval", approval)
    graph.add_edge(START, "approval")
    graph.add_edge("approval", END)
    return graph.compile(checkpointer=checkpointer)


@pytest.mark.asyncio
async def test_rls_and_context_reset(runtime_settings):
    """两个可信主体并发使用连接池，裸查询仍受 RLS 限制，事务退出后身份清除。"""
    async with Resources(runtime_settings) as resources:
        a = Principal("test-" + str(uuid4()), "a", frozenset())
        b = Principal("test-" + str(uuid4()), "b", frozenset())
        first = await resources.data.create_thread(a)
        await resources.data.create_thread(b)

        async def query(principal):
            """故意省略应用级所有权谓词，验证数据库 RLS 本身有效。"""
            async with resources.db.transaction(principal) as session:
                return (await session.scalars(select(Thread))).all()

        rows_a, rows_b = await asyncio.gather(query(a), query(b))
        assert {row.subject_id for row in rows_a} == {"a"}
        assert {row.subject_id for row in rows_b} == {"b"}
        with pytest.raises(AppError):
            await resources.data.get(Thread, first["id"], b)
        async with resources.db.engine.connect() as connection:
            from sqlalchemy import text

            count = (await connection.execute(text("SELECT count(*) FROM threads"))).scalar_one()
            assert count == 0


@pytest.mark.asyncio
async def test_langgraph_pause_resume_and_duplicate_claim(runtime_settings):
    """真实检查点人工等待、重复领取、线程竞争和事件回放。"""
    async with Resources(runtime_settings) as resources:
        resources.agents.register(
            AgentRegistration("contract-graph", "langgraph", graph_factory, GraphInput)
        )
        principal = Principal(
            "test-" + str(uuid4()), "owner", frozenset({"resources:read", "resources:write"})
        )
        thread = await resources.data.create_thread(principal)
        run = await resources.runs.submit(principal, "contract-graph", thread["id"], {"value": 1})
        with pytest.raises(AppError):
            await resources.runs.submit(principal, "contract-graph", thread["id"], {"value": 2})
        await resources.runs.execute(run["id"])
        state = await resources.data.get(Run, run["id"], principal)
        assert state["status"] == "waiting_input", state
        assert await resources.runs.claim(run["id"]) is None
        await resources.runs.respond(
            principal, run["id"], {"value": {"increment": 2}}, state["event_sequence"]
        )
        with pytest.raises(AppError):
            await resources.runs.respond(
                principal, run["id"], {"value": {"increment": 3}}, state["event_sequence"]
            )
        await resources.runs.execute(run["id"])
        result = await resources.data.get(Run, run["id"], principal)
        assert result["status"] == "succeeded", result
        assert result["output"]["value"] == 3
        events = await resources.runs.events(principal, run["id"])
        assert [event["sequence"] for event in events] == list(range(1, len(events) + 1))
        assert await resources.runs.claim(run["id"]) is None
        await resources.data.delete_thread(principal, thread["id"])


def test_api_auth_memory_artifacts_and_ready(runtime_settings, tmp_path):
    """真实基础设施下验证 API Key、CRUD、附件授权和就绪状态。"""
    token = "test-api-" + str(uuid4())
    overrides = runtime_settings.model_dump(exclude={"config_sources"})
    overrides["auth"] = {
        "api_keys": [
            {
                "digest": hashlib.sha256(token.encode()).hexdigest(),
                "tenant_id": "test-" + str(uuid4()),
                "subject_id": "api",
                "scopes": ["resources:read", "resources:write"],
            }
        ]
    }
    overrides["storage"] = {"directory": tmp_path}
    with TestClient(create_app(overrides)) as client:
        assert client.get("/health/live").status_code == 200
        assert client.get("/health/ready").status_code == 200
        assert client.get("/api/v1/agents").status_code == 401
        headers = {"Authorization": "Bearer " + token}
        assert client.get("/api/v1/agents", headers=headers).json() == {"items": []}
        memory = client.put(
            "/api/v1/memories",
            headers=headers,
            json={"namespace": "notes", "key": "decision", "content": "已确认规则"},
        )
        assert memory.status_code == 200, memory.text
        updated = client.put(
            "/api/v1/memories",
            headers=headers,
            json={"namespace": "notes", "key": "decision", "content": "更新规则"},
        )
        assert updated.json()["version"] == 2
        artifact = client.post(
            "/api/v1/artifacts",
            headers=headers,
            files={"file": ("note.txt", "实际内容".encode(), "text/plain")},
        )
        assert artifact.status_code == 201, artifact.text
        download = client.get("/api/v1/artifacts/" + artifact.json()["id"], headers=headers)
        assert download.content == "实际内容".encode()


@pytest.mark.asyncio
async def test_llamaindex_context_pause_and_resume(runtime_settings):
    """使用真实 Workflow 与 Context，跨资源生命周期恢复人工响应。"""
    from workflows import Context, Workflow, step
    from workflows.events import (
        Event,
        HumanResponseEvent,
        InputRequiredEvent,
        StartEvent,
        StopEvent,
    )

    class Awaiting(Event):
        """步骤间原生事件，序列化器负责保存类型和数据。"""

        value: int

    class NativeWorkflow(Workflow):
        """仅在验收期间使用的原生持久化契约。"""

        @step
        async def begin(self, context: Context, event: StartEvent) -> Awaiting:
            """真实状态写入，验证步骤结束后的 Context 快照。"""
            await context.store.set("value", event.value)
            return Awaiting(value=event.value)

        @step
        async def await_input(self, context: Context, event: Awaiting) -> InputRequiredEvent:
            """产生原生人工输入事件，不占用后台执行资源。"""
            return InputRequiredEvent(prefix="需要人工输入", value=event.value)

        @step
        async def finish(self, context: Context, event: HumanResponseEvent) -> StopEvent:
            """恢复后读取持久状态，验证原生响应流。"""
            value = await context.store.get("value")
            return StopEvent(result={"value": value + event.increment})

    def factory(context, checkpointer):
        """每次恢复创建新 Workflow 实例，状态来自持久 Context。"""
        return NativeWorkflow(timeout=10)

    principal = Principal(
        "test-" + str(uuid4()), "llama", frozenset({"resources:read", "resources:write"})
    )
    async with Resources(runtime_settings) as resources:
        resources.agents.register(
            AgentRegistration("contract-workflow", "llamaindex", factory, GraphInput)
        )
        thread = await resources.data.create_thread(principal)
        run = await resources.runs.submit(
            principal, "contract-workflow", thread["id"], {"value": 2}
        )
        await resources.runs.execute(run["id"])
        result = await resources.data.get(Run, run["id"], principal)
        assert result["status"] == "waiting_input", result
        await resources.runs.respond(
            principal, run["id"], {"value": {"increment": 4}}, result["event_sequence"]
        )
    async with Resources(runtime_settings) as resources:
        resources.agents.register(
            AgentRegistration("contract-workflow", "llamaindex", factory, GraphInput)
        )
        await resources.runs.execute(run["id"])
        result = await resources.data.get(Run, run["id"], principal)
        assert result["status"] == "succeeded", result
        assert result["output"] == {"value": 6}


@pytest.mark.asyncio
async def test_cancellation_lease_and_tool_ledger(runtime_settings, tmp_path):
    """真实数据库验证取消、租约回收、写入账本与未知副作用不会盲目重试。"""
    import time
    from datetime import timedelta

    from app.db.models import ToolCall, now
    from app.runtime.contracts import RunContext, current_run

    settings = runtime_settings.model_copy(deep=True)
    settings.tools.directory = tmp_path
    async with Resources(settings) as resources:
        resources.agents.register(
            AgentRegistration("contract-graph", "langgraph", graph_factory, GraphInput)
        )
        principal = Principal(
            "test-" + str(uuid4()),
            "ledger",
            frozenset({"resources:read", "resources:write", "tools:files:write"}),
        )
        thread = await resources.data.create_thread(principal)
        run = await resources.runs.submit(principal, "contract-graph", thread["id"], {"value": 1})
        claimed = await resources.runs.claim(run["id"])
        assert claimed is not None
        assert await resources.runs.claim(run["id"]) is None
        context = RunContext(
            principal,
            run["id"],
            thread["id"],
            claimed.lease_token,
            resources,
            time.monotonic() + 30,
            10000,
            None,
            approvals={"write-1": {"approved": True}},
        )
        token = current_run.set(context)
        try:
            parameters = {"path": "note.txt", "content": "写入内容", "operation_id": "write-1"}
            first = await resources.tools.execute("file_write", parameters)
            second = await resources.tools.execute("file_write", parameters)
            assert first == second and (tmp_path / "note.txt").read_text() == "写入内容"
            async with resources.db.transaction(principal) as session:
                row = (
                    await session.scalars(select(ToolCall).where(ToolCall.run_id == run["id"]))
                ).one()
                row.status = "unknown"
            with pytest.raises(AppError, match="核对"):
                await resources.tools.execute("file_write", parameters)
        finally:
            current_run.reset(token)
        async with resources.db.transaction(principal) as session:
            stored = await session.get(Run, run["id"])
            stored.lease_until = now() - timedelta(seconds=1)
        assert await resources.runs.recover() >= 1
        state = await resources.data.get(Run, run["id"], principal)
        assert state["status"] == "queued"
        cancelled = await resources.runs.cancel(principal, run["id"])
        assert cancelled["status"] == "cancelled"
        assert await resources.runs.claim(run["id"]) is None
