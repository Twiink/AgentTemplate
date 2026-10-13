# AgentTemplate

Python 3.12、FastAPI、PostgreSQL、Redis 组成的通用 Agent 后端模板。核心包含配置、启动预检、日志、鉴权、限流、模型接入、Prompt、工具与 MCP、线程与运行管理；附件、后台运行、RAG 知识库和长期记忆做成可开关的模块，按两档预设组合。

默认 Agent 注册表为空。注册项目自己的实现后即可使用运行管理、人工响应、持久事件回放和恢复。基础 API、诊断与存储不依赖模型凭据。

## 两档预设

| 能力 | minimal | full |
|---|---|---|
| 配置、启动预检、日志、鉴权、限流、健康检查、指标 | 有 | 有 |
| 模型、Prompt（YAML front matter）、工具与 MCP、线程与运行记录 | 有 | 有 |
| 预置工具（文件、HTTP + Markdown 提取、搜索、SQL、时间） | 有 | 有 |
| 附件（storage 模块） | 有，本地目录 | 有，本地目录或 S3 |
| 后台运行、Worker、租约恢复（background 模块） | 无 | 有 |
| RAG 知识库（knowledge 模块，混合检索 + RRF） | 无 | 有 |
| 长期记忆（memory 模块，语义检索 + LangGraph store） | 无 | 有 |
| 评测框架（内置评分器、并发执行、基线对比） | 有 | 有 |
| Kubernetes 与观测部署文件 | 无 | 有 |

预设只决定默认启用的模块。之后可以直接修改配置里的 `modules` 列表增减模块；knowledge 依赖 storage 与 background。

## 初始化与本机开发

```bash
uv run agent-template init --preset minimal   # 或 --preset full
uv sync                                       # full 预设执行 uv sync --extra full
docker compose --env-file .data/compose.env up -d postgres redis
uv run agent-template migrate
uv run agent-template preflight
uv run agent-template dev
```

full 预设另外启动 Worker 与调度进程：

```bash
uv run agent-template worker
uv run agent-template scheduler
```

`init` 创建 `config/private.yaml`、`.data/developer.key` 和 `.data/compose.env`，使用随机开发凭据及仅当前用户可读的文件权限。重复执行保留已有配置。加 `--prune` 会删除未启用模块的代码、迁移和用不到的部署文件，删除前列出路径并确认。数据库端口为 5433，Redis 端口为 6380。模型缺省时诊断为未配置；不会调用收费模型做健康检查。

API、Worker 和 scheduler 启动前自动执行启动预检，检查配置、模块、目录、数据库连接与角色、迁移版本、行级安全和 Redis；存在失败的阻断项时拒绝启动，并逐项输出修复建议。`agent-template preflight` 可单独执行同样的检查，`doctor` 在此基础上再检查 Agent、MCP 与各模块状态。

API 文档位于 `/docs`。资源接口使用 `/api/v1`，必须携带 `Authorization: Bearer <凭据>`。私有开发 Key 保存在 `.data/developer.key`，不要将它写入共享配置、文档或日志。身份只能来自已验证凭据。

## 目录

| 位置 | 内容 |
|---|---|
| `src/app/core` | 配置、启动预检、日志、错误、鉴权、限流、指标与追踪、资源容器 |
| `src/app/db` | 数据库会话、核心表、通用仓储、迁移版本链与核心迁移 |
| `src/app/api` | 路由公用依赖、请求上下文中间件、健康检查、`/api/v1` 核心路由 |
| `src/app/schemas` | 核心接口的请求与响应模型 |
| `src/app/services` | 线程、运行、健康检查与评测服务 |
| `src/app/llm`、`src/app/prompts`、`src/app/tools` | 模型接入、Prompt 加载、工具治理与内置工具、MCP |
| `src/app/runtime` | 注册契约与原生框架运行适配 |
| `src/app/modules` | 可选模块：storage、background、knowledge、memory；各自包含表、服务、路由与迁移 |
| `src/app/cli` | `agent-template` 命令 |

## 配置与扩展

配置顺序为基础默认值、预设、环境 YAML、私有 YAML、`AGENT_*` 环境变量、程序显式覆盖。嵌套环境变量使用双下划线，字典和列表使用 JSON。未知字段直接报错。

配置与公共契约详见 [开发指南](docs/开发指南.md)。常用配置包括 `preset`、`modules`、`llm`（模型别名白名单与价格表路径）、`agents`、`auth`、`storage`、`tools`、`mcp` 和 `tracing`。聊天与向量模型按别名声明，只有白名单内的别名才能被引用；业务 Prompt 放在 `prompts` 目录并按内容摘要记录版本。

项目把 Agent 放在 `src/app/agents/<名称>/agent.py` 并导出 `AgentSpec`；启动时自动发现，ID 重复或构建失败都会停止启动。`build(ctx)` 只做装配，不发起网络调用。详情见 [Agent 自动发现](docs/开发指南.md)。

### 注册 Agent 示例

使用 LangChain `create_agent` 快速构建：

```python
# src/app/agents/assistant/agent.py
from langchain.agents import create_agent
from app.agents.registry import AgentSpec

async def build(ctx):
    model = await ctx.models.chat("gpt-4o")
    tools = ctx.tools.langchain(["read_file", "write_file", "http_fetch"])
    prompt = ctx.prompts.render("assistant/system.md", {"role": "助手"})
    
    return create_agent(
        model=model,
        tools=tools,
        system_message=prompt[0],
        checkpointer=ctx.checkpointer,
        store=ctx.store,  # 启用 memory 模块时可用
    )

spec = AgentSpec(
    id="assistant",
    description="通用助手，可读写文件和访问网页",
    build=build,
)
```

使用 LangGraph 编写复杂流程：

```python
# src/app/agents/reviewer/agent.py
from langgraph.graph import StateGraph, START, END
from app.agents.registry import AgentSpec

async def build(ctx):
    model = await ctx.models.chat("claude-3-5-sonnet")
    
    class State(TypedDict):
        messages: Annotated[list, add_messages]
        documents: list[str]
        score: int
    
    graph = StateGraph(State)
    graph.add_node("analyze", lambda s: analyze(s, model))
    graph.add_node("review", lambda s: review(s, model))
    graph.add_edge(START, "analyze")
    graph.add_edge("analyze", "review")
    graph.add_edge("review", END)
    
    return graph.compile(checkpointer=ctx.checkpointer)

spec = AgentSpec(
    id="reviewer",
    description="文档审查流程",
    build=build,
)
```

### 调用 API 示例

流式接口：

```python
import httpx

async with httpx.AsyncClient() as client:
    async with client.stream(
        "POST",
        "http://localhost:8000/api/v1/threads/thread-123/runs/stream",
        headers={"Authorization": f"Bearer {api_key}"},
        json={"agent_id": "assistant", "input": {"messages": [{"role": "user", "content": "你好"}]}},
    ) as response:
        async for line in response.aiter_lines():
            if line.startswith("data: "):
                print(json.loads(line[6:]))
```

后台运行与断线续传（full 档）：

```python
# 提交后台运行
response = await client.post(
    "http://localhost:8000/api/v1/threads/thread-123/runs",
    headers={"Authorization": f"Bearer {api_key}"},
    json={"agent_id": "assistant", "input": {...}},
)
run_id = response.json()["id"]

# 订阅事件流，支持 Last-Event-ID 续传
async with client.stream(
    "GET",
    f"http://localhost:8000/api/v1/runs/{run_id}/stream",
    headers={"Authorization": f"Bearer {api_key}", "Last-Event-ID": "42"},
) as response:
    async for line in response.aiter_lines():
        if line.startswith("data: "):
            event = json.loads(line[6:])
            print(f"{event['event']}: {event['data']}")
```

人工审批后继续：

```python
# 提交审批决定
await client.post(
    "http://localhost:8000/api/v1/threads/thread-123/runs/wait",
    headers={"Authorization": f"Bearer {api_key}"},
    json={
        "agent_id": "assistant",
        "resume": {"action": "approve"},
        "expected_sequence": 5,
    },
)
```

### 配置概览

核心配置字段：

```yaml
# config/base.yaml
preset: minimal  # 或 full
modules: [storage]  # full 为 [storage, background, knowledge, memory]

llm:
  models:
    gpt-4o:
      provider: openai
      model: gpt-4o-2024-08-06
      api_key: ${OPENAI_API_KEY}
    claude-3-5-sonnet:
      provider: anthropic
      model: claude-3-5-sonnet-20241022
      api_key: ${ANTHROPIC_API_KEY}
  embeddings:
    text-embedding-3-small:
      provider: openai
      model: text-embedding-3-small
      dimensions: 1536
  pricing_file: config/pricing.yaml

agents:
  enabled: []  # 空列表表示启用全部自动发现的 Agent
  extraction: memory-extractor  # 记忆提取使用的 Agent（需启用 memory 模块）

auth:
  api_keys:
    - digest: abc123...  # SHA-256 摘要
      tenant_id: default
      subject_id: service-account
      scopes: [resources:read, resources:write, tools:files:read]

tools:
  directory: .data/workspace
  http_allowed_domains: [example.com, api.openai.com]
  sql_readonly_url: ${READONLY_DATABASE_URL}
  sql_allowed_tables: [users, orders]

mcp:
  filesystem:
    command: npx
    args: [-y, "@modelcontextprotocol/server-filesystem", /allowed/path]
    allowed_tools: [read_file, write_file]
```

### 开启观测

LLM 链路追踪（发往 Langfuse 或 LangSmith）：

```yaml
# config/production.yaml
tracing:
  enabled: true
  endpoint: https://cloud.langfuse.com
  headers:
    Authorization: Bearer ${LANGFUSE_SECRET_KEY}
  sample_rate: 0.1
  capture_content: false  # 生产环境建议关闭正文采集
  redaction_patterns: [sk-[a-zA-Z0-9]+]

# 或使用 LangSmith
langsmith:
  enabled: true
  api_key: ${LANGSMITH_API_KEY}
  project: production
```

错误追踪（Sentry）：

```yaml
sentry:
  dsn: ${SENTRY_DSN}
  environment: production
  traces_sample_rate: 0.01
```

Prometheus 指标已内置在 `/metrics`；Grafana 面板和告警规则在 `deploy/observability/`。

### 模板不包含的内容

- **前端界面与调试 UI**：API 优先，界面由项目自行决定。
- **示例 Agent 代码**：注册表默认为空，用法见上方代码片段。
- **测试代码**：测试由派生项目在开发阶段自行添加；模板用启动预检保证配置和依赖正确。
- **多 Agent 编排封装**：直接使用 LangGraph 原生写法。
- **OCR、GraphRAG、计费系统、管理后台**：按需扩展。

## 已接入的基础能力

| 能力 | 入口 |
|---|---|
| API Key／JWT、所有权、RLS | `app.core.security`、`Database.transaction` |
| 原生模型与调用治理 | `container.models` |
| 版本化 Prompt | `container.prompts` |
| 文件、HTTP、搜索、只读 SQL、MCP | `container.tools`、`container.mcp` |
| 线程、运行查询、取消、SSE 回放 | `container.threads`、`container.runs`、`/api/v1/threads`、`/api/v1/threads/{id}/runs/stream` |
| 后台运行（background） | `container.background`、`POST /api/v1/threads/{id}/runs`、`GET /api/v1/runs/{id}/stream` |
| 附件（storage） | `container.artifacts`、`/api/v1/artifacts` |
| 长期记忆（memory） | `container.memories`、`/api/v1/memories` |
| 解析、向量化、知识检索、索引重建（knowledge） | `container.knowledge`、`/api/v1/knowledge` |
| 启动预检 | `app.core.preflight`、`agent-template preflight` |
| 结构化日志与脱敏 | `app.core.logging` |
| 按主体限流 | `app.core.ratelimit` |
| Prometheus、OTLP | `app.core.telemetry` |

工具绑定显式进行；写操作默认需要人工批准并记录副作用账本。结果未知时必须核对，不能因为恢复而直接重复写入。HTTP 默认没有允许目标，SQL 默认没有只读连接，网页搜索默认没有服务商 Key，未配置能力准确拒绝。

## 检查与验收

```bash
uv run agent-template check
uv run agent-template preflight
```

`check` 执行格式、lint、类型与规范检查；类型检查需要安装 full 的依赖（`uv sync --extra full`），或先用 `init --prune` 删除未启用的模块。模板不包含测试代码，测试由派生项目在开发阶段按需添加；模板通过启动预检和冒烟检查确认配置正确、依赖可用。验收步骤见 [验收清单](docs/验收标准.md)。

注释及 docstring 使用中文，公共接口说明用途、边界和副作用，复杂逻辑解释设计理由。项目文本及命令输出禁止 Emoji，规则详见 [AGENTS.md](AGENTS.md)。

## 部署、升级与排错

Compose 按档位拆成三个文件：

```bash
# 两档共用：PostgreSQL、Redis、迁移与 API
docker compose --env-file .data/compose.env up -d --build
# full：追加 Worker 与调度进程
docker compose -f compose.yaml -f compose.full.yaml --env-file .data/compose.env up -d --build
```

可选的 Prometheus、Grafana、Collector 与自托管 Langfuse 在 `compose.observability.yaml`，Kubernetes overlays 在 `deploy/kubernetes`。生产使用非特权数据库连接、外部 PostgreSQL／Redis／S3，并通过 Secrets 注入秘密。数据库迁移单独执行；发现旧版本表或未知迁移版本时停止并输出诊断。

详见 [部署与运维](docs/运维指南.md)。复用模板建立新项目时，重新设置 `.openviking/config.json` 的稳定 `peer.id`，不要继承模板身份。
