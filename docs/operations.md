# 部署与运维

## 生产配置

生产环境 `AGENT_ENVIRONMENT=production`，必须配置 API Key 或 JWT。运行数据库角色不能是超级用户或拥有 BYPASSRLS。迁移角色与运行角色分开；初始化 Compose 脚本创建 `agent_app` 并授予自有表及检查点表的必要访问权限。

生产模型、S3、数据库和 Redis 使用部署系统注入的环境变量或 Secret。`private.yaml` 不打入镜像；容器无 root 权限。镜像依赖由 `uv.lock` 固定，源代码来自当前构建目录。

Compose 文件按档位拆分：`compose.yaml` 包含两档共用的 PostgreSQL、Redis、migrate 与 API；`compose.full.yaml` 追加 Worker、scheduler 与可选的 MinIO；`compose.observability.yaml` 为可选观测栈。镜像依赖由 `.data/compose.env` 中的 `AGENT_EXTRAS` 决定，minimal 为空，full 为 `full`。Compose 先执行 migrate 服务，迁移成功后 API、Worker 和 scheduler 才启动。三类进程启动前都执行启动预检，配置、数据库角色、迁移版本、行级安全或 Redis 存在问题时拒绝启动，日志逐项给出修复建议。scheduler 单实例运行。API 优雅退出 30 秒，Worker 为当前运行留出 360 秒；超时崩溃后租约恢复会重新投递，副作用账本避免未知操作盲目重复。

Kustomize 位于 `deploy/kubernetes/overlays/development` 和 `production`。部署前创建 `agent-secrets`，注入实际外部服务连接和身份配置，替换 `agent-template:0.2.0` 为已发布 SHA 或版本镜像。先运行迁移 Job，成功后发布其余工作负载。S3 模式在基础配置中开启，bucket 等必须实际配置。

## 观测

应用只创建一个 OpenTelemetry Provider 并导出到 Collector。每个进程复用同一 provider，Celery 任务不会在每个资源上下文关闭全局 exporter。Compose 中 API 指标和 Worker 9005 指标端点分别采集，Worker 容器内共享多进程指标目录；不同容器不共享 PID 标识的指标文件。API 到 Worker 的 W3C trace context 保存在运行投递上下文。Collector 中启用的 exporter 必须对应已配置后端；只有一个后端时删去另一项 exporter，防止无效请求。

Langfuse OTLP Authorization 使用项目 public／secret key 的 Basic 编码，LangSmith 使用服务商 Key 和项目 Header。Trace 默认不包含消息和文档正文。启用 `capture_content` 必须配置 `redaction_patterns`。日志由 structlog 与标准库共用一条处理链输出，local 环境默认为控制台格式，其余环境为 JSON；每条日志带 request_id，链路追踪开启时带 trace_id 与 span_id。日志写出前按字段名、内置规则和 `redaction_patterns` 脱敏，异常堆栈同样经过脱敏后记录。

Prometheus 标签仅使用 route 模板、状态、框架、服务商和工具名称。`/metrics` 应由网络或反向代理限制到采集器；它只输出进程内指标，排队与执行中的运行数量由 API 进程每 15 秒采集一次，数据库故障不影响指标端点。详细健康诊断需要 Bearer 身份，存活和就绪探针不调用收费模型。

就绪探针只检查数据库和 Redis 能否连通，结果缓存 2 秒。迁移版本、运行角色等启动条件由启动预检负责，因此先执行迁移 Job 不会让已在运行的实例失去就绪。

## 迁移、备份与恢复

`agent-template migrate` 首先查询实际数据库表和 Alembic 版本。仅空库或本代码迁移版本链中的版本允许迁移；版本链以磁盘上的迁移脚本为准，项目新增的迁移自动纳入。核心迁移与各模块迁移是独立分支，`migrate` 只升级核心分支和已启用模块的分支；之后启用新模块，再执行一次 `migrate` 即可补齐。0.3 重建了迁移基线，0.2 创建的数据库版本不在新版本链中，`migrate` 会拒绝执行，需要重建数据库或人工迁移。旧模板的 `conversations`、`agent_runs` 等结构需要独立迁移方案，命令不会清空、自动降级或猜测列转换。

备份同时覆盖 PostgreSQL、自有附件／S3 和观测后端数据；Redis 不是持久运行状态来源。恢复时先还原数据库与附件，再核对身份、模型空间、配置版本和迁移版本，最后启动 API、Worker 和 scheduler。向量维度不能在恢复时随意改变。

升级前备份并在独立环境运行全部检查和迁移验收。迁移 Job 失败时阻止发布。不要通过手动删除版本表或空跑成功脚本绕过版本检查。

## 故障诊断

| 现象 | 检查 |
|---|---|
| 启动失败 | 日志中的启动预检失败项，或执行 `agent-template preflight` 查看修复建议 |
| 就绪 503 | 数据库与 Redis 能否连通；`doctor` 查看详细诊断 |
| 资源接口 503 且错误码为 rate_limit_unavailable | Redis 是否可用；`rate_limit_failure_mode` 是否为 `closed` |
| 排队不执行 | Worker 与 scheduler 在线、Redis 连接、outbox 是否已投递 |
| 等待输入不恢复 | expected_sequence 是否为当前等待序号，响应是否属于资源所有者 |
| Worker 崩溃 | lease_until、恢复任务、原生检查点与工具账本 |
| 向量检索失败 | 模型配置快照是否改变、维度与活动 index 是否一致 |
| 文件访问拒绝 | 相对目录、链接、大小与工具权限 |
| HTTP 获取拒绝 | allowed_hosts、目标实际 IP、跳转和响应大小 |
| SQL 拒绝 | 专门只读连接、角色特权、事务只读和单条查询 |
| Trace 缺失 | OTLP endpoint、Collector exporter、后端认证和项目名 |

诊断输出不得展示原始配置、密钥、Cookie 或完整连接串。日志中没有正文时，需要由有权开发者在本机结合资源记录进行定位，不在公开诊断接口扩大采集范围。

## 可选观测和存储启动

```bash
uv run agent-template observability-init --langfuse
```

命令创建 0600 权限的 `.data/observability.env` 和 `.data/collector.yaml`。同时启用 LangSmith 时添加 `--langsmith`，随后在私有环境文件中填入实际 Key。已有私有观测文件不会覆盖。

```bash
docker compose -f compose.yaml -f compose.full.yaml -f compose.observability.yaml \
  --env-file .data/compose.env --env-file .data/observability.env \
  --profile langfuse --profile tracing --profile metrics up -d
```

应用设置 `tracing.enabled=true`，本机 endpoint 为 `http://localhost:4318/v1/traces`，容器 endpoint 为 `http://collector:4318/v1/traces`。Langfuse 页面位于本机 3001，Grafana 位于 3000。可选 S3 本机存储在 `compose.full.yaml` 中，使用 `--profile storage` 启动，启用后创建真实 bucket 并配置 `storage.backend=s3`、endpoint、bucket 与私有凭据；模板不伪造 bucket 已存在。

## 评估与可选记忆提取

`agent-template evaluate` 接受 JSONL 数据集、评分器 `模块:函数`、输出文件、可选 baseline 和 threshold。每行数据包含 id、agent_id、input、expected 和 options。评分器接收真实运行记录并返回指标名到有限数值的字典。环境身份用 `AGENT_EVALUATION_TOKEN` 或私有开发 Key 验证。结果记录真实用量缺失、运行失败和评分失败，不预设业务正确率。

`memory_extraction_registration` 默认为空。项目注册真实提取 Agent 后，源运行成功会排队提取；输入包含 source_run_id、input、output，输出必须符合 `ExtractionOutput`。模板筛选常见秘密内容、按 namespace／key 去重合并、保留来源，最多三次失败重试。提取 Agent 自身不会递归触发提取。
