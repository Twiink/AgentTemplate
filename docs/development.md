# 开发契约

## 配置与资源

`Settings` 定义唯一配置模型。`load_settings(overrides, root)` 先深度合并文件再环境变量和显式覆盖。`config_sources` 只用于诊断，不输出值。敏感配置为 `SecretStr`；配置文件不得提交凭据。

FastAPI lifespan 和 Worker 任务使用 `Resources` 上下文，资源消费者先退出，再逆序关闭 MCP、模型 HTTP 客户端、Redis 和数据库。模型与 Checkpointer 惰性创建，`migrate` 负责表结构，API 请求不迁移。

常用字段：

| 字段 | 含义 |
|---|---|
| `database_url` | 非超级用户、无 BYPASSRLS 的运行连接 |
| `migration_database_url` | 单独迁移连接 |
| `redis_url` | Celery 队列、限流与通知 |
| `chat_models`／`embedding_models` | 独立模型配置字典 |
| `default_embedding` | 默认向量配置名称 |
| `registrations` | 项目注册函数引用列表 |
| `auth.api_keys` | Key SHA-256 摘要、稳定 tenant／subject／scopes |
| `auth.jwt_*`／`auth.jwks_url` | 签名算法、密钥或 JWKS、issuer、audience |
| `storage` | 本地或 S3 后端、附件上限 |
| `tools` | 工作目录、允许域名、只读连接及表允许列表、Tavily 凭据与限制 |
| `mcp` | 显式进程或 HTTP 地址、凭据环境引用、工具允许列表 |
| `run_*`／`lease_seconds` | 时间、Token、费用、保留期和租约 |

模型配置必须提供 provider、model、api_key。OpenAI 兼容端点使用 `endpoint`；各服务商原生参数通过 `parameters` 传入。声明 tools、streaming、structured 等能力；请求能力不满足时执行前拒绝。Embedding 需要声明 dimensions，更换参数或模型需重建空间。

## 显式注册

`AgentRegistration` 包含 ID、framework、factory、输入 Schema、可选输出 Schema、capabilities。配置中的注册入口接收 `Resources` 并调用 `resources.agents.register`。默认没有业务注册函数；项目自行实现。

工厂签名为 `factory(context, checkpointer)`，可为异步函数。LangChain 使用原生 `create_agent`，LangGraph 使用原生 `StateGraph.compile`，两者必须使用传入的 Checkpointer。LlamaIndex 返回原生 Workflow／AgentWorkflow，不把网络客户端等资源放入 JSON 状态。

`RunContext` 提供可信身份、运行和线程 ID、资源、截止时间、预算与用量。模型通过 `resources.models.chat(name)`、`llama_chat(name)` 获取，工具通过 `tools.langchain(names)`／`tools.llama(names)` 显式绑定。原生对象可访问，但直接调用原生模型会绕过模板治理，因此项目必须自行承担预算和审计边界。

原生运行配置分别放在 `options.langchain`、`options.langgraph`、`options.llamaindex`。线程、检查点 ID 和 Context 由服务端决定，客户端不得覆盖。

## 运行与恢复

运行提交返回 202。PostgreSQL 同一事务保存运行、线程互斥及 outbox，调度进程发布任务后确认；发布后崩溃可能重复投递，Worker 用运行行锁和 lease_token 避免两个有效消费者。

事件写入在行锁内分配递增序号。SSE 用 `id`、`event` 和单行 JSON `data` 返回，支持 `Last-Event-ID` 和 `after`，心跳不产生业务序号。订阅断开不会取消任务。

LangGraph 使用原生 `interrupt` 和 `Command(resume=...)`。等待响应接口携带 `expected_sequence`，响应结构中 `value` 是原生 resume 值。LlamaIndex 响应同样通过 `value` 发送原生 HumanResponseEvent；步骤完成边界保存 Context，等待输入保存后释放 Worker。

至少一次执行可能重跑正在执行的步骤。写工具需要稳定调用 ID，执行前记录账本，成功保存结果；未知结果由业务核对后处理。项目自己的副作用也应采用同样策略。不要在可重放节点中使用无幂等保障的外部写入。

## 知识、记忆与存储

线程归属、原生检查点、长期记忆和知识文档分别管理。长期记忆按 namespace／key 显式更新并保留版本和来源。`ContextPolicy` 生成裁剪后的上下文，不修改原始消息。

知识库绑定不可变 Embedding 快照。附件上传后提交 import，后台解析 TXT／Markdown／HTML／PDF／DOCX，记录页码或位置及原文件摘要。扫描 PDF 没有可提取文字时返回 `ocr_required`，项目通过 `register_parser` 注册真实 OCR 实现。

新索引重建时旧空间保持活动，所有导入成功后才切换。更换模型后继续使用旧配置将被拒绝，避免向量空间混用。检索提供 source 元数据；LangChain Retriever 与 LlamaIndex Retriever 均使用相同服务和访问控制。

## 工具权限与模型预算

API Key 默认只拥有资源读写。工具 scopes 根据项目显式发放：`tools:files:read`、`tools:files:write`、`tools:http`、`tools:search`、`tools:sql`、`tools:mcp:<服务名>`。路径逐级禁止符号链接；HTTP 逐跳验证域名和实际连接 IP；SQL 使用只读事务与无特权角色。

模型治理限制超时、并发、上下文和输出上限，SDK 负责有限重试，模板不重复重试。真实服务商 usage 与保守字节估算分别记录；缺失真实用量字段为 null。费用要求明确 price_version 和单价，不能把未知费用说成零。

MCP 协议工具默认按副作用处理，只有明确 `readOnlyHint=true` 才按只读治理；项目也可通过 `write_tools` 强制确认。stdio 和 HTTP 会话在专门连接任务内建立和关闭，调用任务不直接退出会话取消作用域。

SQL 工具的 `sql_allowed_tables` 默认为空。只允许访问项目明确声明的表与基本无副作用函数；不要给只读角色授予原生检查点、系统目录或其他租户表的无限读取权限。
