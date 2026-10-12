# AgentTemplate

Python 3.12、FastAPI、PostgreSQL、Redis 组成的通用 Agent 后端模板。核心包含配置、启动预检、日志、鉴权、限流、模型接入、Prompt、工具与 MCP、线程与运行管理；附件、后台运行、RAG 知识库和长期记忆做成可开关的模块，按两档预设组合。

默认 Agent 注册表为空。注册项目自己的实现后即可使用运行管理、人工响应、持久事件回放和恢复。基础 API、诊断与存储不依赖模型凭据。

## 两档预设

| 能力 | minimal | full |
|---|---|---|
| 配置、启动预检、日志、鉴权、限流、健康检查、指标 | 有 | 有 |
| 模型、Prompt、工具与 MCP、线程与运行记录 | 有 | 有 |
| 附件（storage 模块） | 有，本地目录 | 有，本地目录或 S3 |
| 后台运行、Worker、租约恢复（background 模块） | 无 | 有 |
| RAG 知识库（knowledge 模块） | 无 | 有 |
| 长期记忆（memory 模块） | 无 | 有 |
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

配置与公共契约详见 [开发契约](docs/development.md)。常用配置包括 `preset`、`modules`、`llm`（模型别名白名单与价格表路径）、`agents`、`auth`、`storage`、`tools`、`mcp` 和 `tracing`。聊天与向量模型按别名声明，只有白名单内的别名才能被引用；业务 Prompt 放在 `prompts` 目录并按内容摘要记录版本。

项目把 Agent 放在 `src/app/agents/<名称>/agent.py` 并导出 `AgentSpec`；启动时自动发现，ID 重复或构建失败都会停止启动。`build(ctx)` 只做装配，不发起网络调用。详情见 [Agent 自动发现](docs/development.md)。

## 已接入的基础能力

| 能力 | 入口 |
|---|---|
| API Key／JWT、所有权、RLS | `app.core.security`、`Database.transaction` |
| 原生模型与调用治理 | `container.models` |
| 版本化 Prompt | `container.prompts` |
| 文件、HTTP、搜索、只读 SQL、MCP | `container.tools`、`container.mcp` |
| 线程、运行查询、取消、SSE 回放 | `container.threads`、`container.runs`、`/api/v1/threads`、`/api/v1/runs` |
| 后台运行与人工响应（background） | `container.background`、`POST /api/v1/runs` |
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

`check` 执行格式、lint、类型与规范检查；类型检查需要安装 full 的依赖（`uv sync --extra full`），或先用 `init --prune` 删除未启用的模块。模板不包含测试代码，测试由派生项目在开发阶段按需添加；模板通过启动预检和冒烟检查确认配置正确、依赖可用。验收步骤见 [验收清单](docs/acceptance.md)。

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

详见 [部署与运维](docs/operations.md)。复用模板建立新项目时，重新设置 `.openviking/config.json` 的稳定 `peer.id`，不要继承模板身份。
