# AgentTemplate

Python 3.12、FastAPI、PostgreSQL／pgvector、Redis 与 Celery 组成的通用 Agent 工程底座。LangChain、LangGraph 和 LlamaIndex 分别使用自己的原生对象、状态和配置，共享资源、模型、工具、运行管理与交付设施。

默认 Agent 注册表为空。注册项目自己的实现后即可使用后台运行、人工响应、持久事件回放和恢复。基础 API、诊断、Worker 与存储不依赖模型凭据。Worker 的任务包括运行、知识导入、可选记忆提取、投递、租约恢复和保留期清理。

## 初始化与本机开发

```bash
uv sync --locked
uv run agent-template init
docker compose --env-file .data/compose.env up -d postgres redis
uv run agent-template migrate
uv run agent-template doctor
```

分别启动三个进程：

```bash
uv run agent-template dev
uv run agent-template worker
uv run agent-template scheduler
```

`init` 创建 `config/private.yaml`、`.data/developer.key` 和 `.data/compose.env`，使用随机开发凭据及仅当前用户可读的文件权限。重复执行保留已有配置。数据库端口为 5433，Redis 端口为 6380。模型缺省时诊断为未配置；不会调用收费模型做健康检查。

API 文档位于 `/docs`。资源接口使用 `/api/v1`，必须携带 `Authorization: Bearer <凭据>`。私有开发 Key 保存在 `.data/developer.key`，不要将它写入共享配置、文档或日志。身份只能来自已验证凭据。

## 配置与扩展

配置顺序为基础默认值、环境 YAML、私有 YAML、`AGENT_*` 环境变量、程序显式覆盖。嵌套环境变量使用双下划线，字典和列表使用 JSON。未知字段直接报错。

配置与公共契约详见 [开发契约](docs/development.md)。常用配置包括 `chat_models`、`embedding_models`、`registrations`、`auth`、`storage`、`tools`、`mcp` 和 `tracing`。聊天与向量模型单独注册；业务 Prompt 放在 `prompts` 目录并按内容摘要记录版本。

项目通过 `registrations` 中的 `模块:函数` 入口注册 `AgentRegistration`；入口接收 `Resources`。工厂接收 `RunContext` 和模板 Checkpointer，返回原生图或 Workflow。API 和 Worker 使用同样的注册配置。详情见 [框架与恢复约定](docs/development.md)。

## 已接入的基础能力

| 能力 | 入口 |
|---|---|
| API Key／JWT、所有权、RLS | `app.core.security`、`Database.transaction` |
| 原生模型与调用治理 | `resources.models` |
| 版本化 Prompt | `resources.prompts` |
| 文件、HTTP、搜索、只读 SQL、MCP | `resources.tools`、`resources.mcp` |
| 后台运行、人工响应、取消、SSE 回放 | `resources.runs`、`/api/v1/runs` |
| 线程、长期记忆、附件 | `resources.data` |
| 解析、向量化、知识检索、索引重建 | `resources.knowledge` |
| 日志、Prometheus、OTLP | `app.core.observability` |

工具绑定显式进行；写操作默认需要人工批准并记录副作用账本。结果未知时必须核对，不能因为恢复而直接重复写入。HTTP 默认没有允许目标，SQL 默认没有只读连接，网页搜索默认没有服务商 Key，未配置能力准确拒绝。

## 检查与验收

```bash
uv run agent-template check
uv run pytest -q -m integration
```

测试验证配置、凭据、路径、解析、真实 PostgreSQL RLS、原生检查点、人工响应与 API。集成测试需要真实 PostgreSQL 和 Redis；没有配置时明确跳过。外部服务商、观测后端和集群验收必须使用实际可用环境，不能将跳过记录为通过。当前验证结果见 [验收记录](docs/acceptance.md)。

注释及 docstring 使用中文，公共接口说明用途、边界和副作用，复杂逻辑解释设计理由。项目文本及命令输出禁止 Emoji，规则详见 [AGENTS.md](AGENTS.md)。

## 部署、升级与排错

完整 Compose 启动命令：

```bash
docker compose --env-file .data/compose.env up -d --build
```

基础服务、可选存储、Prometheus／Grafana、Collector 配置以及 Kustomize overlays 位于 `deploy`。生产使用非特权数据库连接、外部 PostgreSQL／Redis／S3，并通过 Secrets 注入秘密。数据库迁移单独执行；发现旧模板表或未知迁移版本时停止并输出诊断。

详见 [部署与运维](docs/operations.md)。复用模板建立新项目时，重新设置 `.openviking/config.json` 的稳定 `peer.id`，不要继承模板身份。
