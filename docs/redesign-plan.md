# 两档预设重构计划

- 状态：决策已确认，等待按阶段实施。
- 日期：2026-10-11。
- 版本：0.2.0 升级到 0.3.0。

本文记录 2026-10-11 审查的结论、已确认的决策、目标架构和分阶段实施步骤。实施过程中如需调整，先更新本文，再修改代码。

## 实施进度

| 阶段 | 状态 | 说明 |
|---|---|---|
| 1 | 代码完成，待验证 | 已通过 ruff、ty 与中文规范检查；依赖删减后需执行 `uv lock`，再按第 6 节在完整环境中验证 |
| 2 | 代码完成，待验证 | 同上。`AgentSpec` 调整到 3.2；minimal 目前仍需要 Redis，改为可选放在 5.6 与 5.8 |
| 3 到 7 | 未开始 | 无 |

## 1. 背景

本仓库定位为通用 Agent 后端开发模板：新项目复制后只写业务，不必重复搭建工程底座。审查对照了两类参考：Obsidian 资料库 `AI全栈` 中与 Agent 工程相关的章节，以及 GitHub 上主流的 Agent 模板与框架（见附录 B）。结论如下：

- 方向正确，以下做法予以保留：
  - PostgreSQL 作为持久状态来源；
  - 显式注册、人工介入（HITL）；
  - OTel 与 Prometheus；
  - 严格的配置校验；
  - RLS 与事务级身份；
  - 工具安全边界（路径、SSRF、SQL）与写操作副作用账本；
  - 运行租约与 fencing；
  - 统一错误结构；
  - 非 root 镜像与中文规范检查。
- 存在会直接影响使用者的缺陷，见附录 A 的 P0。
- 结构没有为分档做准备：
  - LangChain、LangGraph、LlamaIndex 三个框架各有一套模型、工具和运行适配；
  - 部分治理能力是自研的，而 LangChain v1 已经内置。
- 作为模板，后端常用能力与开发体验存在缺口。

## 2. 目标与非目标

### 2.1 目标

1. 提供 minimal 和 full 两档预设，开新项目时一条命令选择。minimal 只依赖 PostgreSQL 即可启动。
2. 修复附录 A 中的全部 P0 缺陷。
3. Agent 层统一为 LangChain `create_agent`，复杂流程使用 LangGraph；LlamaIndex 只用于 RAG 数据侧。
4. 预置后端常用能力：
   - 配置分层与启动预检、健康检查；
   - 结构化日志、指标与告警、链路追踪、错误追踪；
   - 鉴权与 API Key 管理；
   - 限流与配额、缓存、幂等。
5. 预置 Agent 常用能力：
   - 模型网关、Prompt 管理；
   - 工具治理与常用工具、MCP；
   - 长期记忆、RAG；
   - 人工审批、流式输出；
   - 评测工具。
6. 新项目照着 README 操作，十分钟内能跑起 minimal 档，并清楚知道业务代码应该放在哪里。

### 2.2 非目标

模板不包含以下内容，README 中也会写明：

- 前端界面与调试 UI。
- 示例 Agent 代码。用法只在 README 中以代码片段说明。
- 测试代码与测试基础设施。测试由派生项目在开发阶段自行决定；模板用启动预检保证配置正确、依赖可用。
- copier 等模板生成器。
- 多 Agent 编排的二次封装。README 给出 LangGraph 的原生写法。
- OCR、GraphRAG、计费系统、管理后台。

## 3. 决策

### 3.1 已确认

| 编号 | 问题 | 结论 | 说明 |
|---|---|---|---|
| D0 | 模板形态 | 分 minimal 与 full 两档预设 | 参考资料主张不要过度设计；主流模板的做法是最小核心加可选项 |
| D1 | 两档如何实现 | 模块化 + `init --preset` | 仓库保持为可运行代码；full 档能力放在 `src/app/modules/`；`--prune` 删除未启用的模块 |
| D2 | full 档后台任务 | 保留 Celery 并修正 | worker 进程常驻事件循环和资源容器；事务提交后直接投递，outbox 只做兜底 |
| D3 | 测试 | 模板不包含测试 | 删除 `tests/` 和 pytest 配置，用启动预检和冒烟检查代替 |
| D4 | minimal 是否启用 RLS | 两档都启用 | minimal 默认单租户；init 自动创建迁移角色和运行角色 |
| D5 | MCP 接入 | 改用官方 `langchain.mcp.MCPAdapter` | 目前为 beta，只在 `tools/mcp.py` 中使用 |
| D6 | LlamaIndex 的定位 | 只用于 RAG 数据侧 | 删除 LlamaIndex 的模型、工具和运行适配 |
| D7 | Agent 框架 | LangChain `create_agent` + LangGraph | `create_agent` 返回 LangGraph 图，运行时只维护一套 |
| D8 | 示例 | 不放示例代码 | README 用代码片段说明 |
| D9 | 前端 | 不做 | 无 |
| D10 | 实施范围 | P0 到 P4 全部纳入 | 按第 5 节分阶段实施 |

### 3.2 默认做法

以下事项没有单独确认，按下表执行。如需调整，直接修改本节。

| 主题 | 默认做法 |
|---|---|
| 错误约定 | HTTP 状态码 + 字符串错误码，错误体为 `{"error": {...}}`；成功响应不额外包装 |
| minimal 的运行方式 | 只提供同步执行（流式返回、等待结果）。客户端断开时默认取消；检查点保留，之后可恢复 |
| 流事件传输 | full 使用 Redis Streams；minimal 在请求内直接推送 |
| 长期记忆 | LangGraph `AsyncPostgresStore` |
| LLM 追踪 | 按 OTel GenAI 语义约定埋点，经 Collector 发往 Langfuse 或 LangSmith，也可只开 LangSmith 原生开关；默认不采集正文 |
| API Key | 前缀 + SHA-256 存入数据库，由 CLI 管理；`init` 生成的开发 Key 仍写在私有配置中 |
| token 计数 | 默认使用不联网的估算：中日韩字符按 1 个 token 计，其他字符按 4 个字符计 1 个 token，每条消息另加固定开销；可切换为 tiktoken |
| 关键词召回 | `pg_trgm`；中文要求更高时，按文档替换为 zhparser 或 BM25 扩展 |
| 评测 | 评测 CLI 和评分器保留，供派生项目使用；不附带用例和数据集 |
| 数据库迁移 | 0.3.0 重建迁移基线，核心和各模块分别是独立分支；`migrate` 检测到 0.2 数据库时拒绝执行 |
| 版本 | 升到 0.3.0，在 `CHANGELOG.md` 记录破坏性变更 |

## 4. 目标架构

### 4.1 两档能力

| 能力 | minimal | full |
|---|---|---|
| 配置分层、启动预检、结构化日志、统一错误、健康检查、指标端点 | 有 | 有 |
| 鉴权（API Key、JWT）与限流 | 有；没有 Redis 时，限流改在进程内计数 | 有 |
| 模型网关、Prompt、工具治理与预置工具、MCP | 有 | 有 |
| LangGraph 运行：流式、等待结果、人工审批、PG Checkpointer | 有 | 有 |
| 多租户归属字段与 PostgreSQL RLS | 有，默认单租户 | 有 |
| 附件存储（storage 模块） | 本地 | 本地或 S3 |
| Redis | 可选：限流、缓存、配额 | 必需：流事件、限流、缓存、配额、Celery broker |
| 后台运行、租约、崩溃恢复、断线续传（background 模块） | 无 | 有 |
| 长期记忆（memory 模块） | 无 | 有 |
| RAG（knowledge 模块） | 无 | 有 |
| LLM 链路追踪 | 配置开关 | 配置开关，另提供自托管 Langfuse |
| Prometheus、Grafana、告警规则、Collector | 无 | 有 |
| Kubernetes（Kustomize） | 无 | 有 |
| 评测 CLI | 有 | 有，另支持把分数回写到 trace |
| 进程 | api | api、worker、scheduler |

模块可以单独开关，但必须满足依赖关系：
- background 需要 Redis；
- knowledge 需要 background 和 storage；
- memory 的记忆提取功能需要 background。

启动预检会校验这些依赖。

### 4.2 目录结构

```
AgentTemplate/
├── src/app/
│   ├── main.py                  加载配置，执行启动预检，按启用模块装配应用
│   ├── core/                    两档共用的底座，不依赖业务
│   │   ├── config.py            Settings、配置分层、presets 与模块开关
│   │   ├── preflight.py         启动预检的注册与执行
│   │   ├── logging.py           structlog 配置、上下文字段、脱敏、异常格式化
│   │   ├── errors.py            AppError、错误码目录、异常处理器
│   │   ├── security.py          API Key、JWT、Principal、scopes
│   │   ├── ratelimit.py         限流与配额
│   │   ├── telemetry.py         Prometheus 指标、OTel、LLM 埋点、Sentry
│   │   ├── cache.py             Redis 缓存与可选的 LLM 缓存
│   │   ├── idempotency.py       Idempotency-Key
│   │   └── container.py         类型化资源容器与生命周期
│   ├── db/
│   │   ├── session.py           引擎与带身份的事务
│   │   ├── base.py              Base 与归属混入
│   │   ├── rls.py               迁移中使用的 RLS 策略与授权辅助函数
│   │   ├── repository.py        带租户条件和游标分页的通用仓储
│   │   └── migrations/          核心迁移；模块迁移放在各自模块目录
│   ├── api/
│   │   ├── deps.py              身份、限流、服务等依赖
│   │   ├── middleware.py        请求上下文、访问日志、指标
│   │   └── v1/                  agents.py threads.py runs.py health.py
│   ├── schemas/                 核心接口的请求与响应模型
│   ├── services/                threads.py runs.py（同步执行路径）
│   ├── llm/
│   │   ├── gateway.py           init_chat_model、别名白名单、能力校验、降级与重试
│   │   ├── pricing.py           价格表读取
│   │   ├── usage.py             用量与预算回调、配额计数、指标
│   │   └── tokens.py            token 计数
│   ├── prompts/                 Prompt 加载器；模板文件放在仓库根目录的 prompts/
│   ├── tools/
│   │   ├── governance.py        @governed_tool：scope、写操作、超时、审批、副作用账本
│   │   ├── builtin/             files.py http.py search.py sql.py clock.py
│   │   └── mcp.py               MCPAdapter 封装、白名单、读写分类
│   ├── runtime/
│   │   ├── executor.py          LangGraph 执行、预算、超时、取消
│   │   ├── events.py            事件协议与事件总线接口
│   │   ├── checkpointer.py      基于连接池的 AsyncPostgresSaver
│   │   ├── middleware.py        默认中间件预设
│   │   └── context.py           运行上下文（context_schema）
│   ├── agents/                  项目自己的 Agent 放在这里
│   │   └── registry.py          AgentSpec、BuildContext、自动发现
│   ├── modules/
│   │   ├── storage/             附件：本地与 S3、附件接口
│   │   ├── background/          Celery、后台运行、租约、恢复、outbox、Redis Streams、续传接口
│   │   ├── knowledge/           LlamaIndex 导入、pgvector 混合检索、rerank、检索工具
│   │   └── memory/              AsyncPostgresStore、记忆工具、记忆接口、提取任务
│   └── cli/                     init、preflight、doctor、migrate、dev、apikey、config、eval
├── prompts/                     Prompt 模板文件，只放约定说明
├── evals/                       评测约定说明，不附带数据集
├── config/
│   ├── base.yaml  local.yaml  test.yaml  staging.yaml  production.yaml
│   ├── presets/                 minimal.yaml  full.yaml
│   └── pricing.yaml
├── deploy/
│   ├── kubernetes/              full 档
│   ├── observability/           full 档：Prometheus、Grafana、告警、Collector
│   └── postgres/init.sh
├── compose.yaml                 基础服务：postgres、redis（可选）、migrate、api
├── compose.full.yaml            worker、scheduler、minio
├── compose.observability.yaml   Prometheus、Grafana、Collector、Langfuse
├── docs/                        architecture、configuration、api、operations、development、recipes/
├── scripts/                     check_conventions.py、git-hooks
├── AGENTS.md  CLAUDE.md  README.md  CHANGELOG.md
└── Dockerfile  Makefile  pyproject.toml  uv.lock
```

业务代码放在哪里：

| 要做的事 | 位置 |
|---|---|
| 新增 Agent | `src/app/agents/<名称>/agent.py`，导出 `spec` |
| 新增工具 | 通用工具放 `src/app/tools/`，只给某个 Agent 用的放在该 Agent 目录；一律使用 `@governed_tool` |
| 新增 Prompt | `prompts/<agent>/<名称>.md` |
| 新增业务表 | 模型放所属模块；迁移放对应的 `migrations/`；按 `docs/recipes/add-table.md` 配置 RLS 和授权 |
| 新增业务模块 | `src/app/modules/<名称>/`，按 4.3 节的模块契约实现 |
| 评测 | 数据集放 `evals/datasets/`，评分器放 `evals/scorers/` |

### 4.3 模块机制

每个模块在 `src/app/modules/<名称>/__init__.py` 中导出一个 `module` 对象，契约如下：

| 字段 | 作用 |
|---|---|
| `name` | 模块名，对应配置中 `modules` 列表的取值 |
| `requires` | 依赖的其他模块和基础设施，例如 background 需要 Redis |
| `settings` | 模块的配置模型，挂在 `Settings.<名称>` 下 |
| `extra` | 需要安装的依赖 extras 名称 |
| `migrations` | 迁移目录，作为 Alembic 的独立分支，分支标签为模块名 |
| `routers` | 注册到 `/api/v1` 下的路由 |
| `startup`、`shutdown` | 生命周期钩子，按模块依赖顺序执行 |
| `preflight` | 模块自带的启动预检项 |
| `tasks`、`schedules` | Celery 任务与定时任务，依赖 background |
| `tools` | 提供给 Agent 的工具，例如 `knowledge_search`、`memory_search` |
| `commands` | 追加的 CLI 命令 |

应用的装配顺序：
1. 加载配置。
2. 解析启用的模块，校验依赖。
3. 初始化日志。
4. 执行启动预检。
5. 建立核心资源：数据库、Redis、HTTP 客户端、模型网关、Checkpointer。
6. 按依赖顺序启动模块。
7. 发现并构建 Agent。
8. 注册路由，开始服务。

两档的默认模块：

| 预设 | modules |
|---|---|
| `config/presets/minimal.yaml` | `[storage]`，存储后端为本地 |
| `config/presets/full.yaml` | `[storage, background, knowledge, memory]` |

`agent-template init --preset minimal` 或 `--preset full`，可加 `--prune`，依次执行：

1. 写入 `config/private.yaml`：预设名、数据库与 Redis 连接、开发 API Key 的摘要，文件权限 0600。已有文件不覆盖，与现在一致。
2. 生成 `.data/compose.env`，其中包含随机生成的数据库密码。
3. 打印按预设安装依赖的命令：minimal 为 `uv sync`，full 为 `uv sync --extra full`。init 运行在虚拟环境内部，不改动自己所在的环境。
4. 提示是否重置 `.openviking/config.json` 的 `peer.id`，确认后才重置。
5. 加 `--prune` 时，删除以下文件：
   - 未启用模块的目录及其迁移；
   - 用不到的 compose 文件；
   - minimal 额外删除 Kubernetes 和观测相关的部署文件。

   只删除文件，不改动数据库。
6. 打印下一步要执行的命令。

依赖分组，对应 `pyproject.toml` 中的 optional-dependencies：

| 分组 | 内容 |
|---|---|
| 核心依赖 | FastAPI、SQLAlchemy、asyncpg、Alembic、psycopg 连接池、Redis 客户端、structlog、prometheus-client、OTel SDK、LangChain（含 mcp extra）、LangGraph、PostgreSQL Checkpointer、各服务商集成、内置工具所需的解析库 |
| `background` | Celery |
| `knowledge` | llama-index-core、llama-index-readers-file、pgvector、pypdf、python-docx |
| `s3` | boto3 |
| `tracing` | OTel 自动埋点、OpenInference 的 LangChain 埋点 |
| `sentry` | sentry-sdk |
| `full` | 以上全部 extras 的组合；minimal 只安装核心依赖 |

memory 模块使用 LangGraph 自带的 PostgreSQL Store，不需要额外依赖。

迁移规则：
- 核心迁移放在 `src/app/db/migrations/`，各模块的迁移放在各自模块目录下，作为 Alembic 的独立分支。
- `migrate` 只升级核心分支和已启用模块的分支。
- 识别数据库版本时，读取磁盘上所有模块的版本链。这样停用某个模块后，已有的版本仍能被识别。
- 停用模块不会删除它的表。

### 4.4 Agent 注册契约

`src/app/agents/registry.py` 定义下面的契约，项目在 `src/app/agents/<名称>/agent.py` 中导出 `spec`：

```python
@dataclass(frozen=True)
class AgentSpec:
    id: str
    description: str
    build: Callable[[BuildContext], CompiledStateGraph | Awaitable[CompiledStateGraph]]
    input_schema: type[BaseModel] | None = None  # 缺省为 messages 输入
    context_schema: type | None = None           # 在模板运行上下文之外追加的字段


@dataclass(frozen=True)
class BuildContext:
    settings: Settings
    models: ModelGateway             # 按别名获取模型
    tools: ToolRegistry              # 按名称获取 LangChain 工具
    prompts: PromptLoader
    checkpointer: AsyncPostgresSaver
    store: BaseStore | None          # 启用 memory 模块时可用
    middleware: MiddlewarePresets    # 默认中间件组合
```

规则：

- `build` 只负责装配，不发起网络调用。启动时对每个启用的 Agent 构建一次，任何一个构建失败就停止启动。
- 必须使用传入的 `checkpointer`，与现有约束一致。
- 启动时自动发现 `app.agents.*.agent` 中的 `spec`。可以用配置项 `agents.enabled` 限定启用哪些 Agent；ID 重复时启动失败。
- 运行时的身份信息（tenant、subject、run_id、thread_id、scopes）由服务端写入运行上下文，工具通过 `ToolRuntime.context` 读取。客户端和模型都不能覆盖这些信息。

### 4.5 运行链路与接口

| 接口 | 档位 | 说明 |
|---|---|---|
| `GET /api/v1/agents` | 两档 | 列出已启用的 Agent 及其输入结构 |
| `POST/GET/DELETE /api/v1/threads[/{id}]` | 两档 | 线程 |
| `GET /api/v1/threads/{id}/state` | 两档 | 线程的原生状态 |
| `POST /api/v1/threads/{id}/runs/stream` | 两档 | 同步执行，以 SSE 返回事件 |
| `POST /api/v1/threads/{id}/runs/wait` | 两档 | 同步执行，返回最终结果 |
| `POST /api/v1/threads/{id}/runs` | full | 提交后台运行，返回 202 |
| `GET /api/v1/runs`、`GET /api/v1/runs/{id}` | 两档 | 运行记录、用量和结果 |
| `GET /api/v1/runs/{id}/stream` | full | 订阅事件，支持用 `Last-Event-ID` 续传 |
| `POST /api/v1/runs/{id}/cancel` | 两档 | 取消运行 |
| `/api/v1/artifacts` | storage | 附件 |
| `/api/v1/knowledge` | knowledge | 知识库 |
| `/api/v1/memories` | memory | 长期记忆 |
| `/health/live`、`/health/ready`、`/health/details`、`/metrics` | 两档 | 健康检查与指标 |

人工审批后继续执行：
- 通过上面三个创建运行的接口之一提交，请求体传 `resume` 而不是 `input`，同时带上 `expected_sequence` 防止重复提交。
- 审批决定的格式与 `HumanInTheLoopMiddleware` 一致，分为 approve、edit、reject。
- 原有的 `/runs/{id}/responses` 接口删除。

事件协议，即 SSE 的 `event` 字段：

| 事件 | 含义 | 是否落库 |
|---|---|---|
| `run.status` | 运行状态变化：queued、running、waiting_input、succeeded、failed、cancelled | 是 |
| `message.delta` | 文本增量 | 否，只推送 |
| `tool.started`、`tool.completed` | 工具名称、call_id、状态、耗时 | 是 |
| `interrupt` | 等待人工输入，附中断请求内容 | 是 |
| `custom` | 节点或工具通过 `stream_writer` 发出的自定义事件 | 否 |
| `error` | 错误码与说明 | 是 |
| `run.completed` | 运行结束，附最终输出 | 是 |

minimal 的执行流程：

```
客户端 --POST runs/stream--> API
  API 校验输入，锁定线程，写入运行记录
  executor 在请求内执行 LangGraph，Checkpointer 使用连接池
  事件直接写入 SSE，关键事件同时写入 run_events
  客户端断开时默认取消执行；检查点保留，可以用 resume 继续
```

full 的后台流程：

```
客户端 --POST threads/{id}/runs--> API --事务提交后--> send_task --> Redis --> worker
  投递失败的任务留在 outbox，由 scheduler 每 30 秒补发一次

worker 进程启动时建立常驻事件循环和资源容器，之后一直复用
  领取运行（租约 + fencing）
  executor 执行；文本增量合并后写入 Redis Streams，关键事件写入 run_events

客户端 --GET runs/{id}/stream（带 Last-Event-ID）--> API --XREAD BLOCK--> Redis Streams
  Redis 中的事件流过期后，改为返回 run_events 和最终结果
```

### 4.6 治理分工

| 能力 | create_agent 写法 | 自定义 StateGraph 写法 | 位置 |
|---|---|---|---|
| 模型调用次数上限 | `ModelCallLimitMiddleware` | `recursion_limit` | `runtime/middleware.py` |
| 工具调用次数上限 | `ToolCallLimitMiddleware` | `recursion_limit` | `runtime/middleware.py` |
| 模型降级与重试 | `ModelFallbackMiddleware`、`ModelRetryMiddleware` | 网关返回已套上 `with_fallbacks`、`with_retry` 的模型 | `llm/gateway.py` |
| 工具重试与错误回传 | `ToolRetryMiddleware`、工具错误中间件 | `@governed_tool` 捕获异常并返回结构化结果 | `tools/governance.py` |
| 上下文控制 | `SummarizationMiddleware`、`ContextEditingMiddleware`，token 计数统一使用 `llm/tokens.py` | 在节点内自行处理 | `runtime/middleware.py` |
| 写操作审批 | `HumanInTheLoopMiddleware`，`interrupt_on` 按工具元数据自动生成 | `@governed_tool` 在工具内部调用 `interrupt` | `tools/governance.py` |
| PII 处理 | `PIIMiddleware`，可选 | 无 | `runtime/middleware.py` |
| token、费用、时长预算 | 模板的预算回调（`raise_error=True`） | 同左 | `llm/usage.py` |
| 副作用账本（以 `tool_call_id` 实现幂等） | 模板自研 | 同左 | `tools/governance.py` |
| 路径、SSRF、SQL 防护 | 模板自研 | 同左 | `tools/builtin/` |

补充说明：
- 中间件审批通过后，会在运行上下文中记下对应的 `tool_call_id`；`@governed_tool` 发现已审批，就不再中断，避免同一操作审批两次。
- 中间件的类名和回调的 `raise_error` 行为，以实施时安装的版本为准，实施前先核实。

### 4.7 启动预检

预检在三个场景执行：
- API 和 worker 启动时自动执行。阻断项失败时进程退出，并打印修复建议。
- 手动执行 `agent-template preflight [--json] [--probe]`。
- `agent-template doctor` 在预检的基础上，再主动探测 worker、MCP 连接、S3 和模型端点。

| 类别 | 检查内容 | 级别 |
|---|---|---|
| 配置 | 配置来源与加载顺序、未知字段、必填项、预设与模块的依赖关系、模块所需的 extras 是否已安装 | 阻断 |
| 环境变量 | 当前环境要求的变量是否存在（生产环境的连接串和密钥必须由环境变量或 Secret 注入）；local 环境下 `.env` 的加载情况 | 阻断 |
| 生产安全 | 是否使用开发凭据、CORS 是否为 `*`、`/docs` 是否开放、开启正文采集时是否配置了脱敏 | 生产环境阻断，其他环境警告 |
| 目录与文件 | `.data`、工具工作目录、prompts 目录存在且权限正确；私有配置文件权限为 0600 | 阻断或警告 |
| PostgreSQL | 能否连通、版本、所需扩展（pgvector、pg_trgm）；运行角色不是超级用户且没有 BYPASSRLS；RLS 已启用 | 阻断 |
| 迁移 | 数据库版本属于代码的版本链；有未执行的迁移时提示执行 `migrate` | 默认阻断，可配置为警告 |
| Redis | full 档必需且必须能连通；minimal 只在配置了 Redis 时检查 | 阻断或警告 |
| 模型 | 每个别名的配置是否完整：provider、model、密钥来源、能力声明、价格。默认不发起收费调用，加 `--probe` 才探测端点 | 阻断或警告 |
| Agent | 自动发现成功、ID 不重复、每个启用的 Agent 都能构建 | 阻断 |
| 工具与 MCP | 工具名称不重复；MCP 的 stdio 命令存在、环境变量引用存在、URL 合法。不启动 MCP 进程 | 阻断或警告 |
| 模块 | 各模块自带的检查，例如 knowledge 的 Embedding 维度、storage 的目录可写性和 bucket 配置 | 阻断或警告 |

每一项输出名称、结果（通过、警告、失败）、说明和修复建议。`--json` 用于 CI。

### 4.8 可观测性约定

日志：

- 本地环境输出带颜色的控制台格式，其他环境输出 JSON。uvicorn、SQLAlchemy、Celery 等标准库日志统一经过 structlog 输出。
- 每条日志带 `request_id`、`trace_id`、`span_id`、`tenant_id`、`subject_id`、`key_prefix`。与运行相关的日志另带 `run_id`、`agent_id`。上游传入的 `X-Request-ID` 格式合法时沿用，否则重新生成。
- 脱敏处理器按两种规则脱敏：一是字段名（authorization、api_key、password、token、cookie、secret 等），二是配置中的正则表达式。消息正文和文档正文默认不写入日志。
- 错误日志记录异常类型和堆栈，堆栈先经过脱敏处理器。
- SQL 执行时间超过配置阈值时记录慢查询日志，只记语句模板，不记参数。

指标（标签中不包含租户和用户）：

| 指标 | 标签 |
|---|---|
| `agent_http_requests_total`、`agent_http_request_duration_seconds` | method、route、status |
| `agent_llm_requests_total`、`agent_llm_request_duration_seconds`、`agent_llm_time_to_first_token_seconds` | model、provider、status |
| `agent_llm_tokens_total` | model、type（input、output、cached） |
| `agent_llm_cost_total` | model |
| `agent_llm_fallbacks_total` | from_model、to_model |
| `agent_tool_calls_total`、`agent_tool_duration_seconds` | tool、status |
| `agent_runs_total`、`agent_run_duration_seconds` | agent、status |
| `agent_run_queue_wait_seconds`、`agent_queue_depth`、`agent_active_runs` | 无（仅 full） |
| `agent_db_pool_connections` | state（in_use、idle） |
| `agent_ratelimit_rejections_total`、`agent_ratelimit_backend_errors_total` | route |

告警规则（阈值取自参考资料）：
- 5 分钟内错误率超过 5%；
- 非流式接口的 P95 延迟超过 2 秒；
- 模型调用错误率异常升高；
- 队列深度持续增长；
- worker 心跳停止；
- 数据库连接池接近耗尽。

链路追踪：
- 按 OTel GenAI 语义约定埋点，span 属性包含 Agent ID、运行 ID、线程 ID、模型别名和 Prompt 版本。
- 默认不采集正文。开启正文采集时必须配置脱敏规则，并且采集内容真正经过这些规则处理。

## 5. 实施阶段

共 7 个阶段，按顺序实施。

依赖关系：
- 1 → 2 → 3 → 4 是硬依赖。阶段 3 需要阶段 2 定下的模块装配方式；阶段 4 需要阶段 3 定下的事件协议。
- 阶段 5 和阶段 6 只依赖阶段 3，彼此独立，可以互换顺序或按项目需要合并。
- 阶段 7 最后做。

拆分原则：阶段 2 只搬代码、不改行为；所有行为改动集中在阶段 3 及之后。模板不包含测试（D3），只能靠预检和冒烟检查验证，把搬动和改写混在一个阶段里，出了回归将无法定位原因。

每个阶段结束后按第 6 节验证并汇报结果。提交和推送只在明确要求时进行。

### 阶段 1：基线修复与启动预检

目标：修复改动位置独立的 P0 缺陷，建立日志体系和启动预检，按 D3 移除测试。本阶段不改变目录结构和运行方式。工作量：中。

| 任务 | 内容 | 完成标准 |
|---|---|---|
| 1.1 迁移版本链（P0-1） | `migrate` 改为用 `ScriptDirectory.walk_revisions()` 读取版本链 | 在仓库外的临时副本中新增一个迁移，连续执行两次 `migrate` 都成功；未知版本仍会被拒绝 |
| 1.2 就绪探针（P0-2） | ready 只检查数据库和 Redis 能否连通，结果缓存 2 秒；版本兼容检查移到启动预检（1.7） | 执行新迁移后，已在运行的进程 ready 状态不受影响 |
| 1.3 token 计数（P0-4） | 新增 `core/tokens.py`，按 3.2 节的方式计数（阶段 3 迁到 `llm/tokens.py`）；预算按服务商返回的真实 usage 累计；`http_fetch` 先加上截断 | 对 1 万汉字的对话，估算值与 tiktoken（o200k_base）的计数相差不超过 50%；默认配置下不再误判超限 |
| 1.4 日志体系（P0-5、P2-1） | 新增 `core/logging.py`，按 4.8 节实现；运行超时归为 `run_timeout`；`log_level` 改为枚举校验 | 人为触发一个异常，日志中有 request_id 和脱敏后的堆栈；uvicorn 和 SQLAlchemy 的日志格式一致 |
| 1.5 限流解耦（P0-6） | 新增 `core/ratelimit.py`，作为独立依赖。Redis 不可用时按配置决定：fail-open 放行，fail-closed 拒绝；两种情况都记录指标 | 停掉 Redis 后，fail-open 时请求正常处理并产生指标，fail-closed 时返回 503 和明确的错误码，都不再出现 500 |
| 1.6 指标采集（P0-8） | 队列深度等指标由后台任务每 15 秒采集一次，`/metrics` 只读取内存中的值 | 停掉 PostgreSQL 后，`/metrics` 仍返回 200 |
| 1.7 启动预检第一版（P2-9） | 新增 `core/preflight.py` 和 `preflight` 命令，覆盖配置、环境变量、目录、PostgreSQL、迁移、Redis；接入 lifespan，阻断项失败即退出；`doctor` 在预检基础上扩展 | 缺少数据库配置、迁移未执行、运行角色是超级用户时，启动直接退出并给出修复提示 |
| 1.8 移除测试（D3） | 删除 `tests/`（4 个文件）、pytest 相关的依赖和配置、Makefile 中的测试目标；`check` 命令不再运行 pytest；CI 去掉 pytest 步骤，改为加入预检；重写 `docs/acceptance.md`。完整的两档 CI 矩阵在 5.7 完成 | `agent-template check` 只运行格式、lint、类型和规范检查，并且全部通过；CI 中不再引用 pytest |

### 阶段 2：结构与模块机制

目标：目录结构、模块装配、两档机制和 Agent 注册契约全部定型。本阶段只搬代码、新增装配层和配置机制，不改变任何具体行为。工作量：大。

| 任务 | 内容 | 完成标准 |
|---|---|---|
| 2.1 目录与容器重组（P1-2） | 按 4.2 节迁移代码；`core/container.py` 取代 `Resources` 这类服务定位写法；路由按资源拆分并补上 `response_model`；服务改由 `Depends` 注入 | 现有接口在新结构下行为不变；OpenAPI 文档中能看到响应结构 |
| 2.2 模块机制与两档（P1-1，D1） | 实现 4.3 节的模块契约、presets、`init --preset` 与 `--prune`、依赖 extras、compose 拆分、Alembic 分支与按分支升级、0.3 迁移基线。`AgentSpec` 会把构建时机从每次运行改为启动时，属于行为改动，放在 3.2 | 干净副本上依次执行 init minimal、启动基础 compose、migrate、dev，只靠 PostgreSQL 即可启动；full 档同样能启动；minimal 执行 `--prune` 后应用仍能启动 |
| 2.3 清理死代码（P0-9 的一部分） | 删除 5 个空包、`scripts/` 下的兼容脚本、Redis `publish` 等无订阅方的死代码 | ruff 不再报告未使用的代码；仓库中没有只含 `__init__.py` 的包。`Telemetry.content()` 在 5.2 接通，`ContextPolicy` 在 3.4 由摘要中间件取代，本阶段不动 |

### 阶段 3：Agent 层重写

目标：从模型接入到请求内执行，跑通一次完整的 Agent 调用。工作量：大。这是行为改动集中的阶段。

| 任务 | 内容 | 完成标准 |
|---|---|---|
| 3.1 模型网关（P1-7） | 用 `init_chat_model` 创建模型；新增 `openai_compatible` 类型；`api_key` 改为可选；只允许使用白名单内的别名；价格表放在 `config/pricing.yaml`；相关配置统一到 `llm.models` 和 `llm.embeddings`；把 `core/tokens.py` 迁到 `llm/tokens.py` | 预检能列出每个别名的配置状态；本地 Ollama 或 vLLM 不配置密钥也能通过校验 |
| 3.2 框架收敛与注册契约（P1-3，D6，D7） | 删除 `llm/llama.py`、`LlamaIndexAdapter`、`llama_chat`、`llama_embedding`、`tools.llama()`、`knowledge.llama_retriever()`；实现 4.4 节的 `AgentSpec` 与自动发现，删除 `registrations` 配置和 `framework` 字段 | knowledge 模块之外不再 import `llama_index`；核心依赖中没有 LlamaIndex |
| 3.3 运行时与同步接口（P1-8，P1-9，P0-3 的 minimal 部分） | 实现 `runtime/executor.py`、事件协议、`runs/stream` 与 `runs/wait`、通过 `resume` 继续执行、线程互斥；Checkpointer 改用连接池；fence 锁使用独立连接；客户端断开时默认取消 | 用不调用模型的临时图（验证后删除）验证流式、等待结果、人工中断与恢复、取消；同时运行 20 个任务，连接池不会耗尽 |
| 3.4 治理迁移（P1-4，P1-5，D5） | 实现中间件预设和预算回调；身份改用 `context_schema` 传递；实现 `@governed_tool`，副作用账本以 `tool_call_id` 为键；接入 HITL 中间件；用 `MCPAdapter` 重写 `tools/mcp.py`，保留白名单和读写分类；删除 `operation_id`、`GovernedChatModel` 和 `ContextPolicy` | create_agent 和自定义 StateGraph 两种写法下，分别验证：预算超限时中止、写工具需要审批、恢复后不重复执行写操作；MCP 工具的 schema 保留嵌套结构和字段描述 |

### 阶段 4：后台运行

目标：full 档的异步链路可用。工作量：中大。

| 任务 | 内容 | 完成标准 |
|---|---|---|
| 4.1 background 模块（P0-7，P0-3 的 full 部分，D2） | Celery 在 `worker_process_init` 时建立常驻事件循环和资源容器；事务提交后投递，outbox 每 30 秒补发一次；文本增量合并后写入 Redis Streams；SSE 用 `XREAD BLOCK` 读取，支持 `Last-Event-ID`；迁移现有的租约、心跳、恢复、取消逻辑 | worker 日志显示每个进程只初始化一次资源；本机从提交到出现 `running` 事件不超过 200 毫秒；断线重连后从断点继续接收事件；杀掉 worker 后，租约到期时运行自动恢复 |

### 阶段 5：后端常用能力

目标：补齐 P2。工作量：中到大。

| 任务 | 内容 | 完成标准 |
|---|---|---|
| 5.1 指标与告警（P2-2） | 按 4.8 节补齐指标；更新 Grafana 面板和告警规则 | `/metrics` 包含 4.8 节列出的全部指标；Prometheus 加载告警规则时不报错 |
| 5.2 LLM 链路追踪（P2-3、P0-9 的一部分） | 接入 FastAPI、httpx、SQLAlchemy、Redis、Celery 的自动埋点，以及 OpenInference 的 LangChain 埋点；接通 `Telemetry.content()` 的正文采集与脱敏；保留 LangSmith 原生开关，但两条路径不能同时开启 | 开启后，Langfuse 中能看到运行、模型、工具三层 span，默认不含正文；开启正文采集后，脱敏规则生效 |
| 5.3 错误追踪（P2-4） | 可选接入 Sentry，development 环境默认关闭 | 配置 DSN 后，未处理的异常会被上报 |
| 5.4 鉴权与安全（P2-5） | 新增 `api_keys` 表和 `apikey create/list/revoke` 命令，日志中只记录 Key 前缀；加入生产安全检查；生产环境默认关闭 `/docs`；`/health/details` 不再启动 MCP 进程 | 新建的 Key 只显示一次；吊销后立即失效；生产配置中有开发凭据时，预检阻断启动 |
| 5.5 错误码目录（P2-6） | 在 `core/errors.py` 中集中定义错误码，并在 `docs/api.md` 中列出 | 代码中不再有分散定义的错误码字符串 |
| 5.6 限流、配额、缓存、幂等（P2-7） | 限流改为 GCRA 算法，支持按路由配置；minimal 未配置 Redis 时限流改在进程内计数；按租户统计每日 token 配额；提供缓存工具，LLM 缓存可选；`Idempotency-Key` 记录存入 PostgreSQL | 超出限流时返回 429 并带 `Retry-After`；用同一个 `Idempotency-Key` 重复提交时返回首次的结果；配额用完时返回明确的错误码 |
| 5.7 配置与工程化（P2-8） | 新增 `config show` 命令，脱敏后打印实际生效的配置；local 环境允许显式指定 `.env`；CI 按 minimal 和 full 两档矩阵执行安装、ruff、ty、规范检查、锁文件检查、镜像构建、compose 启动、migrate、预检、健康检查；kind 集群只在 `deploy/` 有改动时启动；加入 gitleaks、pip-audit 和镜像扫描 | 两档的 CI 都通过 |
| 5.8 预检扩展（P2-9） | 加入各模块的预检项；minimal 未配置 Redis 时不再把 Redis 作为阻断项；`doctor --probe` 主动探测外部服务 | 停掉某个依赖服务后，预检能给出明确的失败项 |

### 阶段 6：Agent 能力

目标：补齐 P3。工作量：大。

| 任务 | 内容 | 完成标准 |
|---|---|---|
| 6.1 长期记忆（P3-1） | memory 模块基于连接池使用 `AsyncPostgresStore`；命名空间由服务端按租户、用户和类型生成；用 Embedding 别名建立语义索引；支持 TTL；提供 `memory_save`、`memory_search`、`memory_delete` 工具；在 Store 之上重建 `/memories` 接口；改造记忆提取任务；提供一次性命令，把旧 `memories` 表的数据迁入 Store | 不同租户互相看不到对方的记忆；Agent 内能使用记忆工具；迁移命令重复执行时不会重复写入 |
| 6.2 Prompt 管理（P3-2） | 支持 front matter，记录用途、变量和模型建议；以内容哈希作为版本号；版本写入运行记录和 trace；可选同步到 Langfuse | 参数不匹配时报错；运行记录和 trace 中都能看到 Prompt 版本 |
| 6.3 预置工具（P3-3） | 完善 `@governed_tool`；`http_fetch` 用 trafilatura 提取正文并转为 Markdown，按配置截断；搜索支持 Tavily、博查和 SearXNG，可插拔；新增时间工具；工具出错时以结构化结果返回；默认限制工具调用次数 | 抓取普通网页时返回 Markdown 正文，长度不超过截断上限；切换搜索服务只需改配置 |
| 6.4 RAG（P3-4） | 用 LlamaIndex 的 Reader、NodeParser 和 IngestionPipeline 导入，按内容哈希去重；保留版本化索引、重建后发布和 RLS；`pg_trgm` 关键词召回与向量召回用 RRF 融合；定义 rerank 插件接口，并内置一个 HTTP 重排服务的适配；提供带引用来源的 `knowledge_search` 工具；Embedding 通过模型网关获取 | md、pdf、docx 都能导入并记录页码；混合检索的结果带来源；关闭 rerank 时正常工作；更换 Embedding 后仍会提示需要重建 |
| 6.5 评测（P3-5） | 约定 `evals/` 目录结构；提供 `agent-template eval --tags smoke`；内置确定性评分器：精确匹配、包含、正则、JSON Schema、工具调用轨迹；LLM 评分可选；保留基线对比、阈值和分数回写；不附带数据集 | 用仓库外的临时数据集跑通，报告中包含指标、失败原因和与基线的差异 |

### 阶段 7：文档与发布

目标：补齐 P4，发布 0.3.0。工作量：中。

| 任务 | 内容 | 完成标准 |
|---|---|---|
| 7.1 AGENTS.md（P4-1） | 写成约 100 行的索引：项目目标、两档说明、目录地图、常用命令、编码约定（中文注释、不使用 Emoji）、安全红线、扩展文档链接；`CLAUDE.md` 引用它 | 规范检查通过，篇幅在 100 行左右 |
| 7.2 README（P4-2） | 内容：概览、两档的选择、minimal 快速开始、full 启动、目录地图；用代码片段演示注册 `create_agent`、用 LangGraph 编写复杂流程、知识检索工具、调用 API（流式、恢复、后台运行与续传）；配置概览；如何开启观测；模板不包含哪些内容 | 在干净环境中照着 README 操作，十分钟内跑起 minimal |
| 7.3 docs（P4-2） | 新增 `architecture.md`、`configuration.md`、`api.md`；更新 `operations.md` 和 `development.md`；在 `recipes/` 下写新增 Agent、工具、Prompt、表、模块、迁移和模型服务商的操作步骤 | 每篇的操作步骤都能照着执行成功 |
| 7.4 一键启动（P4-3） | Makefile 新增 `setup`（用 `PRESET` 变量选档）、`up`、`down`、`logs`、`preflight` 等目标；init 中加入重置 peer.id 的提示 | 一条命令完成 init、compose、migrate 和 preflight |
| 7.5 发布 | 编写 `CHANGELOG.md`，版本号改为 0.3.0；更新 `docs/acceptance.md` | 第 7 节列出的破坏性变更全部写入 CHANGELOG |
| 7.6 可选：编码助手 skills | 在 `.claude/skills/` 下提供新增 Agent、工具、模块、迁移四个 skill | 确认需要后再做 |

## 6. 验证方式

模板不包含测试代码（D3）。每个阶段用以下方式验证：

1. 静态检查：`ruff format --check`、`ruff check`、`ty check`、`scripts/check_conventions.py`、`uv lock --check`。
2. 启动预检：minimal 和 full 各执行一次 `agent-template preflight`。
3. 冒烟检查：用 compose 启动对应档位，执行 `migrate`，然后访问 `/health/ready`、创建线程、调用流式接口。
4. 链路验证：需要模型参与的部分，有真实凭据时优先使用真实凭据；没有凭据时，在仓库外的临时脚本里用 LangChain 自带的假模型验证接线。临时脚本不提交。

每个阶段结束时汇报三点：执行了哪些检查，结果如何，哪些项目没能验证以及原因。

## 7. 破坏性变更（0.2.0 到 0.3.0）

| 范围 | 变化 |
|---|---|
| 配置 | 删除 `registrations`，改为自动发现加 `agents.enabled`；`chat_models` 和 `embedding_models` 移到 `llm.models` 和 `llm.embeddings`；新增 `preset` 和 `modules` |
| 注册契约 | `AgentRegistration`（含 `framework` 字段）改为 `AgentSpec`；工厂签名改为 `build(ctx)` |
| 框架 | 删除 LlamaIndex Workflow 运行时，以及 LlamaIndex 的模型和工具适配 |
| 接口 | `POST /api/v1/runs` 改为 `/threads/{id}/runs`、`/threads/{id}/runs/stream`、`/threads/{id}/runs/wait`；`/runs/{id}/events` 改为 `/runs/{id}/stream`；删除 `/runs/{id}/responses`，改为创建运行时传 `resume` |
| 工具 | 写工具的 schema 不再包含 `operation_id`，幂等键改为 `tool_call_id` |
| 数据库 | 重建迁移基线；`migrate` 检测到 0.2 数据库时拒绝执行，需要重建数据库或人工迁移；旧 `memories` 表的数据可以用一次性命令迁入 Store |
| 测试 | 删除 `tests/` 和 pytest 配置 |
| 部署 | `docker-compose.yml` 拆分为 `compose.yaml`、`compose.full.yaml`、`compose.observability.yaml` |

## 8. 风险与应对

| 风险 | 应对 |
|---|---|
| 阶段 2 和阶段 3 改动面大，容易引入回归 | 阶段 2 只搬代码、不改行为（2.1），阶段 3 再改行为；每个任务完成后都按第 6 节验证 |
| `MCPAdapter` 是 beta，后续 API 可能变化 | 只在 `tools/mcp.py` 一处使用；锁定依赖版本；升级时只需修改这一个文件 |
| Celery 常驻事件循环的线程安全 | 资源只在事件循环所在线程内创建和使用；任务用 `run_coroutine_threadsafe` 提交并等待结果；worker 关闭时按顺序释放资源 |
| 模板不含测试，缺少回归保护 | 启动预检覆盖配置和依赖；CI 对两档都做冒烟检查；派生项目在开发阶段自行补充测试 |
| Store 和 Checkpointer 的表不受 RLS 约束 | 命名空间和线程标识由服务端根据可信身份生成，客户端和模型都不能指定；在文档中写明这条边界 |
| 中文关键词召回的效果一般 | 默认使用 `pg_trgm`；文档说明如何替换为 zhparser 或 BM25 扩展 |
| 删除 `framework` 字段等破坏性变更 | 版本升到 0.3.0，在 CHANGELOG 中写清迁移方法 |

## 附录 A：审查问题清单

代码位置以 2026-10-11 的 `master`（c89644f）为准。

### P0 缺陷

| 编号 | 问题 | 代码位置 | 阶段 |
|---|---|---|---|
| P0-1 | `migrate` 写死了 0001 到 0003 三个版本。项目新增自己的迁移并执行一次后，再执行 `migrate` 会被拒绝 | `src/app/cli.py:142-149` | 1.1 |
| P0-2 | 就绪探针要求数据库迁移版本与代码完全一致。按流程先执行迁移 Job 时，旧 Pod 会同时变为未就绪；而且每次探测都要重新加载迁移目录 | `src/app/services/health.py:35-41` | 1.2 |
| P0-3 | 流式输出每个 token 写一次数据库（一次带 `FOR UPDATE` 的事务加一次 INSERT）；SSE 每个连接每秒执行 3 个事务；Redis `publish` 没有订阅方 | `src/app/runtime/adapters.py:74-84`、`src/app/services/runs.py:85-99`、`src/app/api/routes.py:202-221` | 3.3、4.1 |
| P0-4 | 用 JSON 字节数当作 token 数，而中文被转义成 6 个字节。默认配置下，对话超过约 4600 个汉字就会被判为超限 | `src/app/services/models.py:81`、`src/app/services/models.py:114` | 1.3 |
| P0-5 | 不记录异常堆栈；request_id 没有进入日志上下文；标准库日志不经过 structlog；运行超时被归为 `execution_failed`；`log_level` 写成小写会导致启动失败 | `src/app/core/observability.py:41-52`、`src/app/core/errors.py:50-54`、`src/app/services/runs.py:322-341`、`src/app/main.py:56` | 1.4 |
| P0-6 | 限流写在身份依赖里，Redis 故障时所有带认证的请求都返回 500 | `src/app/core/security.py:90-98` | 1.5 |
| P0-7 | Celery 的每个任务都调用 `asyncio.run` 并重建全部资源；beat 每 2 秒触发一次投递；一次运行要等 0 到 2 秒才开始执行 | `src/app/worker/celery.py:26-51`、`src/app/services/runs.py:429-472` | 4.1 |
| P0-8 | `/metrics` 每次被抓取都查询数据库，数据库故障时指标端点也跟着失败 | `src/app/main.py:100-116` | 1.6 |
| P0-9 | `Telemetry.content()` 没有任何调用点，`capture_content` 实际不起作用；`ContextPolicy` 没有被使用；存在 5 个空包 | `src/app/core/observability.py:89-95`、`src/app/services/context.py`、`src/app/agent/`、`src/app/api/middleware/`、`src/app/repositories/`、`src/app/schemas/` | 2.3、3.4、5.2 |

### P1 结构

| 编号 | 问题 | 代码位置 | 阶段 |
|---|---|---|---|
| P1-1 | 两档预设没有模块边界，所有能力默认开启 | 全局 | 2.2 |
| P1-2 | 路由中直接开启事务；`routes.py` 共 462 行，且没有 `response_model`；`Resources` 被整个传入各个服务，靠函数内 import 绕开循环依赖 | `src/app/api/routes.py:373-402`、`src/app/core/resources.py:69-85` | 2.1 |
| P1-3 | LangChain、LangGraph、LlamaIndex 三个框架并存，各有一套模型、工具和运行适配 | `src/app/services/llama_models.py`、`src/app/runtime/adapters.py:134-211`、`src/app/services/models.py:244-338`、`src/app/services/tools.py:315-340`、`src/app/services/knowledge.py:577-600` | 3.2 |
| P1-4 | 自研的治理逻辑与 LangChain v1 中间件重叠 | `src/app/services/models.py:18-138`、`src/app/services/tools.py:185-221`、`src/app/services/context.py` | 3.4 |
| P1-5 | 身份靠 ContextVar 传递；写工具要求模型自己填写 `operation_id` | `src/app/runtime/contracts.py:100-102`、`src/app/services/tools.py:184-187`、`src/app/services/mcp.py:129-137` | 3.4 |
| P1-6 | MCP 使用自研客户端，schema 转换只保留顶层类型；直接 import `httpx2`，却没有声明为依赖 | `src/app/services/mcp.py:93-104`、`src/app/services/mcp.py:194-212` | 3.4 |
| P1-7 | 模型只支持三家服务商；`api_key` 必填；没有降级链 | `src/app/core/config.py:40-85` | 3.1 |
| P1-8 | 运行只能走后台，minimal 档无法使用 | `src/app/api/routes.py:127-227` | 3.3 |
| P1-9 | Checkpointer 只用一个连接；advisory lock 在整个运行期间占用一个池连接 | `src/app/core/resources.py:102-117`、`src/app/services/runs.py:196-207`、`src/app/db/session.py:16-21` | 3.3 |

### P2 后端常用能力

| 编号 | 问题 | 代码位置 | 阶段 |
|---|---|---|---|
| P2-1 | 日志体系不完整，详见 P0-5 | `src/app/core/observability.py` | 1.4 |
| P2-2 | 指标只有 HTTP 和调用次数，缺少 token、费用、首 token 耗时、连接池等 | `src/app/core/observability.py:28-38` | 5.1 |
| P2-3 | LLM 追踪只有三类手工 span，不含输入输出；LangSmith 被强制关闭 | `src/app/services/runs.py:260-273` | 5.2 |
| P2-4 | 没有错误追踪 | 无 | 5.3 |
| P2-5 | API Key 只能写在 YAML 中；没有生产环境的默认值检查；`/health/details` 会启动 MCP 进程 | `src/app/core/config.py:20-38`、`src/app/services/health.py:139-144` | 5.4 |
| P2-6 | 参考资料中的错误约定互相矛盾，模板需要定下一种，并维护错误码目录 | `src/app/core/errors.py` | 5.5 |
| P2-7 | 限流是固定窗口，全局只有一个阈值；没有配额、缓存工具，也不支持 `Idempotency-Key` | `src/app/core/security.py:90-98` | 5.6 |
| P2-8 | 没有 presets；`prompts/` 目录不存在；CI 每次 push 都启动 kind 集群；没有安全扫描 | `config/`、`.github/workflows/ci.yml` | 5.7 |
| P2-9 | 缺少启动预检，配置和依赖的问题要等到处理请求时才暴露 | `src/app/services/health.py`、`src/app/cli.py:97-113` | 1.7、5.8 |

### P3 Agent 能力

| 编号 | 问题 | 代码位置 | 阶段 |
|---|---|---|---|
| P3-1 | 长期记忆用的是自建表，只能用 `ILIKE` 检索，Agent 内无法使用 | `src/app/services/data.py:113-169` | 6.1 |
| P3-2 | Prompt 只支持 `str.format`，没有元数据 | `src/app/services/prompts.py` | 6.2 |
| P3-3 | `http_fetch` 原样返回最多 2MB 的 HTML；搜索只支持 Tavily；定义一个工具需要的步骤太多 | `src/app/services/tools.py:418-479` | 6.3 |
| P3-4 | 按固定字符窗口切分，只有向量召回 | `src/app/services/knowledge.py:136-187`、`src/app/services/knowledge.py:422-486` | 6.4 |
| P3-5 | 评测只有 JSONL 数据集加自定义评分函数，没有内置评分器，也没有分层 | `src/app/services/evaluation.py` | 6.5 |

### P4 文档与体验

| 编号 | 问题 | 代码位置 | 阶段 |
|---|---|---|---|
| P4-1 | AGENTS.md 已被删除，但 README 仍然引用它 | `README.md` | 7.1 |
| P4-2 | README 没有说明如何注册 Agent、如何调用 API，也缺少扩展步骤的文档 | `README.md`、`docs/` | 7.2、7.3 |
| P4-3 | 没有一键启动命令；新项目需要手动重置 OpenViking 的 peer.id | `Makefile`、`.openviking/config.json` | 7.4 |
| P4-4 | 按 D3 移除测试代码 | `tests/` | 1.8 |

## 附录 B：参考

### 参考资料

以下路径相对于 `~/obsidianlibrary/AI全栈`：

- `开发进阶与架构/后端进阶/后端工程化/`：项目结构分层、多环境配置、结构化日志、搭建骨架的检查清单
- `开发进阶与架构/架构设计/架构演进路线/架构演进的节奏：不要过度设计`
- `开发进阶与架构/架构设计/高可用架构/`：可观测性、限流与熔断、无状态服务
- `AI应用开发宝典/AI 开发框架：LangChian 与 LangGraph/`：LangChain 生产应用实践、LangGraph 生产应用实践、会话状态与持久运行、工具调用与 Agent
- `AI应用开发宝典/工具调用：给AI插上翅膀/`：Function Call、MCP、Skills
- `AI应用开发宝典/Harness Engineering/`
- `AI应用开发宝典/AI原生语言：Python/FastAPI速通/`
- `实战项目/Agent原生项目-HarnessPRD/`、`实战项目/Audit Harness/`、`实战项目/RAGNexus（RAG中台）/`

### 主流项目

数据于 2026-10-11 用 `gh` 查询。

| 项目 | Star | 借鉴点 |
|---|---|---|
| GoogleCloudPlatform/agent-starter-pack（已进入维护模式，继任者是 google/agents-cli） | 6570 | `--prototype` 生成最小项目；`enhance` 为已有项目补上部署和 CI；生成时可以选择生成 AGENTS.md 等指引文件 |
| wassim249/fastapi-langgraph-agent-production-ready-template | 2697 | FastAPI + LangGraph 的目录分层；Langfuse、Prometheus、限流、评测 |
| vstorm-co/full-stack-ai-agent-template | 1941 | 生成器的开关默认全关；生成后删除未选框架的文件；`make upgrade` 三方合并模板更新 |
| langchain-ai/new-langgraph-project、react-agent、memory-agent | 297、852、482 | 使用 `langgraph.json`；graph、state、context、prompts、tools 分文件 |
| fastapi/full-stack-fastapi-template | 45943 | 默认密钥检查；Sentry；启动前执行迁移 |
| google/adk-python、google/adk-recipes | 21776、10440 | 按目录约定导出 `root_agent`；evalset |
| agno-agi/agentos-docker | 10 | 评测按 smoke、release、live 分层；提供给编码助手的 skills |
| microsoft/agent-framework | 14054 | 按目录自动发现 Agent |
| mastra-ai/mastra | 28703 | 统一的装配入口；scorers；敏感数据过滤 |
| aws/agentcore-cli | 304 | 用 `add` 命令增量添加组件 |
| humanlayer/12-factor-agents、NirDiamant/agents-towards-production | 26642、21632 | 设计原则与生产化教程 |
| langchain-ai/langchain-mcp-adapters | 3653 | 已归档，官方改为 `langchain.mcp.MCPAdapter` |

### 官方文档要点

- LangChain v1 的 `create_agent` 内置多种中间件：工具错误处理、工具重试、模型重试、模型降级、摘要、人工审批、模型与工具调用次数上限、PII、To-do、上下文编辑、子 Agent 等。
- `langchain.mcp.MCPAdapter` 需要 `langchain[mcp]>=1.4.0`，目前是 beta。
- 工具通过 `ToolRuntime` 可以获取 state、context、store、stream_writer、执行信息和 `tool_call_id`。
- 长期记忆：`create_agent(store=...)`，工具内通过 `runtime.store` 读写；生产环境使用 PostgreSQL Store。
