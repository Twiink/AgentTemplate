# 如何新增 Agent

本指南演示如何在 AgentTemplate 中创建和注册新的 Agent。

## 快速开始

### 1. 创建 Agent 目录

```bash
mkdir -p src/app/agents/assistant
```

### 2. 创建 agent.py

```python
# src/app/agents/assistant/agent.py
"""通用助手 Agent：可以读写文件、访问网页和执行搜索。"""

from langchain.agents import create_agent
from app.agents.registry import AgentSpec, BuildContext


def build(ctx: BuildContext):
    """构建 Agent；只做装配，不发起网络调用。"""
    model = ctx.models.chat("openai-main")
    
    # 只绑定此 Agent 需要的工具；调用时仍校验主体权限。
    tools = ctx.tools.langchain([
        "file_read",
        "file_write",
        "http_fetch",
        "web_search",
    ])
    
    system_prompt, _, _ = ctx.prompts.render(
        "assistant/system.md",
        {"role": "助手", "capabilities": "文件读写、网页访问、搜索"}
    )
    
    # 使用 LangChain create_agent
    return create_agent(
        model=model,
        tools=tools,
        system_prompt=system_prompt,
        checkpointer=ctx.checkpointer,  # 必须使用
        store=ctx.store,  # 启用 memory 模块时可用
    )


# 导出 AgentSpec
spec = AgentSpec(
    id="assistant",
    description="通用助手，可读写文件、访问网页和搜索",
    build=build,
)
```

### 3. 创建 Prompt 文件

```bash
mkdir -p prompts/assistant
```

```markdown
<!-- prompts/assistant/system.md -->
---
purpose: 通用助手的系统提示
variables:
  role: 角色描述
  capabilities: 能力列表
recommended_models:
  - gpt-4o
  - claude-3-5-sonnet
temperature: 0.7
---

你是一个{role}，具备以下能力：{capabilities}。

## 职责

1. 理解用户意图，明确需求
2. 使用合适的工具完成任务
3. 给出清晰、准确的回答
4. 遇到不确定的情况时主动询问

## 工具使用原则

- 读文件前先确认路径
- 写文件前说明将要写入的内容
- 访问网页时说明目的
- 搜索时使用准确的关键词

## 输出格式

- 使用 Markdown 格式
- 代码使用代码块
- 重要信息使用粗体
```

### 4. 验证

```bash
# 检查语法
uv run agent-template check

# 启动预检（会自动发现并构建 Agent）
uv run agent-template preflight

# 启动开发服务器
uv run agent-template dev
```

### 5. 测试

```bash
# 创建线程
curl -X POST http://localhost:8000/api/v1/threads \
  -H "Authorization: Bearer $(cat .data/developer.key)" \
  -H "Content-Type: application/json" \
  -d '{"title": "测试助手"}'

# 执行任务（假设线程 ID 为 thread-123）
curl -X POST http://localhost:8000/api/v1/threads/thread-123/runs/wait \
  -H "Authorization: Bearer $(cat .data/developer.key)" \
  -H "Content-Type: application/json" \
  -d '{
    "agent_id": "assistant",
    "input": {
      "messages": [
        {"role": "user", "content": "列出当前目录的文件"}
      ]
    }
  }'
```

## 使用 LangGraph

对于复杂的多步骤流程，使用 LangGraph：

```python
# src/app/agents/reviewer/agent.py
"""代码审查 Agent：分析、审查、生成报告。"""

from typing import TypedDict, Annotated
from langgraph.graph import StateGraph, START, END
from langgraph.graph.message import add_messages
from app.agents.registry import AgentSpec, BuildContext


class ReviewState(TypedDict):
    """审查状态。"""
    messages: Annotated[list, add_messages]
    file_path: str
    issues: list[dict]
    score: int


def build(ctx: BuildContext):
    model = ctx.models.chat("openai-main")
    tools = ctx.tools.langchain(["file_read"])
    
    # 定义节点
    async def analyze(state: ReviewState):
        """分析代码，识别问题。"""
        content = state["messages"][-1].content
        # 调用模型分析...
        return {"issues": []}
    
    async def review(state: ReviewState):
        """审查问题，评分。"""
        # 审查逻辑...
        return {"score": 0}
    
    async def report(state: ReviewState):
        """生成报告。"""
        prompt, _, _ = ctx.prompts.render("reviewer/report.md", {
            "file": state["file_path"],
            "score": str(state["score"]),
            "count": str(len(state["issues"])),
        })
        response = await model.ainvoke([{"role": "user", "content": prompt}])
        return {"messages": [response]}
    
    # 构建图
    graph = StateGraph(ReviewState)
    graph.add_node("analyze", analyze)
    graph.add_node("review", review)
    graph.add_node("report", report)
    
    graph.add_edge(START, "analyze")
    graph.add_edge("analyze", "review")
    graph.add_edge("review", "report")
    graph.add_edge("report", END)
    
    return graph.compile(checkpointer=ctx.checkpointer)


spec = AgentSpec(
    id="reviewer",
    description="代码审查 Agent，分析问题并生成报告",
    build=build,
)
```

## 自定义输入和上下文

### 自定义输入 Schema

```python
from pydantic import BaseModel, Field


class AnalysisInput(BaseModel):
    """分析任务的输入。"""
    file_path: str = Field(description="要分析的文件路径")
    check_types: list[str] = Field(
        default=["syntax", "style", "security"],
        description="检查类型"
    )


spec = AgentSpec(
    id="analyzer",
    description="代码分析器",
    build=build,
    input_schema=AnalysisInput,  # 自定义输入
)
```

调用时：

```json
{
  "agent_id": "analyzer",
  "input": {
    "file_path": "src/main.py",
    "check_types": ["syntax", "security"]
  }
}
```

### 追加运行上下文字段

```python
from typing import TypedDict


class AnalysisContext(TypedDict):
    """分析上下文的额外字段。"""
    project_root: str
    exclude_patterns: list[str]


spec = AgentSpec(
    id="analyzer",
    description="代码分析器",
    build=build,
    context_schema=AnalysisContext,  # 追加字段
)
```

**注意**：身份字段（tenant_id、subject_id）由服务端写入，不能覆盖。

## 集成 MCP 工具

```python
def build(ctx: BuildContext):
    return create_agent(
        model=ctx.models.chat("openai-main"),
        tools=ctx.tools.langchain(["file_read", "file_write", "mcp_github_create_issue"]),
        system_prompt="...",
        checkpointer=ctx.checkpointer,
    )

spec = AgentSpec(
    id="issue-assistant",
    description="使用工作区文件和 GitHub MCP 工具处理问题",
    build=build,
    mcp_servers=("github",),
)
```

声明在 `mcp_servers` 中的服务会在该 Agent 构建前连接；未声明的 MCP 服务不会因其他 Agent 启动而连接。发现到的工具名采用 `mcp_<服务名>_<远端工具名>`。

## 使用长期记忆

```python
def build(ctx: BuildContext):
    model = ctx.models.chat("openai-main")
    tools = ctx.tools.langchain(["file_read"])
    
    # store 在启用 memory 模块时可用
    if ctx.store is None:
        raise RuntimeError("此 Agent 需要 memory 模块")
    
    return create_agent(
        model=model,
        tools=tools,
        system_prompt="...",
        checkpointer=ctx.checkpointer,
        store=ctx.store,  # 启用记忆
    )
```

## 中间件和治理

使用预设中间件：

```python
def build(ctx: BuildContext):
    model = ctx.models.chat("openai-main")
    tools = ctx.tools.langchain(["file_read", "file_write"])
    
    return create_agent(
        model=model,
        tools=tools,
        system_prompt="...",
        checkpointer=ctx.checkpointer,
    )
```

模型超时、预算与用量由模型网关统一处理；工具绑定通过 `ToolRegistry` 执行权限校验、审批和副作用账本。不要绕过注册表直接传入写工具。

## 只启用特定 Agent

编辑 `config/base.yaml` 或环境配置：

```yaml
agents:
  enabled:
    - assistant
    - reviewer
```

空列表表示启用全部：

```yaml
agents:
  enabled: []  # 启用全部自动发现的 Agent
```

## 目录结构建议

```
src/app/agents/
├── assistant/
│   ├── __init__.py
│   ├── agent.py           # 主文件，导出 spec
│   ├── nodes.py           # 节点函数（复杂流程）
│   └── tools.py           # Agent 专用工具
├── reviewer/
│   ├── __init__.py
│   ├── agent.py
│   └── scorers.py         # 评分器
└── registry.py            # AgentSpec 契约

prompts/
├── assistant/
│   ├── system.md
│   └── examples.md
└── reviewer/
    ├── system.md
    └── report.md
```

## 常见问题

### Agent 未被发现

1. 检查文件路径：`src/app/agents/<名称>/agent.py`
2. 检查是否导出了 `spec` 变量
3. 检查 `spec` 是否是 `AgentSpec` 实例
4. 查看启动日志中的 Agent 发现信息

### 构建失败

1. `build()` 函数不能发起网络调用
2. 检查模型别名是否在 `llm.models` 中声明
3. 检查工具名称是否正确
4. 检查 Prompt 文件是否存在

### ID 冲突

每个 Agent 的 `id` 必须唯一。检查是否有重复的 ID。

### 工具权限不足

确保 API Key 或 JWT 包含所需的 scope：

```yaml
auth:
  api_keys:
    - digest: "..."
      scopes:
        - resources:read
        - resources:write
        - tools:files:read    # 需要
        - tools:files:write   # 需要
        - tools:http          # 需要
```

## 下一步

- [新增工具](创建工具指南.md)
- [新增 Prompt](创建Prompt指南.md)
- [Agent 注册契约](开发指南.md#agent-自动发现)
- [BuildContext API](开发指南.md#运行上下文)
