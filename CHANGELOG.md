# Changelog

本文档记录 AgentTemplate 的重要变更。

格式遵循 [Keep a Changelog](https://keepachangelog.com/zh-CN/1.0.0/)，版本号遵循 [语义化版本](https://semver.org/lang/zh-CN/)。

## [0.3.0] - 未发布

### 重大变更（破坏性）

#### 配置结构变更
- **删除** `registrations` 配置，改为自动发现 `src/app/agents/*/agent.py`
- **移动** `chat_models` 和 `embedding_models` 到 `llm.models` 和 `llm.embeddings`
- **新增** `preset` 和 `modules` 字段控制启用的模块
- **新增** `llm.pricing_file` 指定价格表路径（默认 `config/pricing.yaml`）

迁移示例：
```yaml
# 旧配置（0.2.0）
chat_models:
  gpt-4:
    provider: openai
    model: gpt-4

# 新配置（0.3.0）
llm:
  models:
    gpt-4:
      provider: openai
      model: gpt-4
      api_key: ${OPENAI_API_KEY}
```

#### Agent 注册契约变更
- **删除** `AgentRegistration`（含 `framework` 字段）
- **新增** `AgentSpec` 契约
- **变更** 工厂函数签名：`build(ctx: BuildContext) -> CompiledStateGraph`
- **删除** LlamaIndex Workflow 运行时

迁移示例：
```python
# 旧方式（0.2.0）
from app.core.registry import AgentRegistration

registration = AgentRegistration(
    id="assistant",
    framework="langchain",
    factory=build_agent,
)

# 新方式（0.3.0）
from app.agents.registry import AgentSpec

spec = AgentSpec(
    id="assistant",
    description="通用助手",
    build=build_agent,
)
```

#### API 接口变更
- **变更** `POST /api/v1/runs` → `POST /api/v1/threads/{id}/runs`
- **新增** `POST /api/v1/threads/{id}/runs/stream` 流式同步执行
- **新增** `POST /api/v1/threads/{id}/runs/wait` 等待结果
- **变更** `GET /api/v1/runs/{id}/events` → `GET /api/v1/runs/{id}/stream`
- **删除** `POST /api/v1/runs/{id}/responses`（改为创建运行时传 `resume`）

人工审批继续执行的新方式：
```python
# 提交审批决定
POST /api/v1/threads/{thread_id}/runs/wait
{
  "agent_id": "assistant",
  "resume": {"action": "approve"},
  "expected_sequence": 5
}
```

#### 工具幂等键变更
- **删除** 写工具 schema 中的 `operation_id` 字段
- **变更** 幂等键改为使用 LangGraph 原生的 `tool_call_id`
- 副作用账本自动基于 `tool_call_id` 实现，无需工具显式声明

#### 数据库迁移
- **重建** 迁移基线（从 0001 重新开始）
- **变更** 核心迁移和模块迁移分别作为独立 Alembic 分支
- **不兼容** 0.2.0 数据库需要重建或人工迁移

**注意**：`migrate` 命令检测到 0.2 数据库时会拒绝执行并提示。

#### 测试框架
- **删除** `tests/` 目录和 pytest 配置
- **变更** 使用启动预检和冒烟检查代替单元测试
- 派生项目可自行添加测试框架

#### Docker Compose
- **拆分** `docker-compose.yml` 为：
  - `compose.yaml`（基础：postgres、redis、migrate、api）
  - `compose.full.yaml`（full 档追加：worker、scheduler）
  - `compose.observability.yaml`（可选：prometheus、grafana、collector、langfuse）

### 新增功能

#### 两档预设
- **minimal 档**：只依赖 PostgreSQL，快速原型
  - 核心功能 + 同步执行 + 本地附件
- **full 档**：完整生产能力
  - minimal 功能 + 后台运行 + Worker + 知识库 + 长期记忆

#### 启动预检
- 自动检查配置、环境变量、目录、数据库、迁移版本、Redis
- 阻断项失败时拒绝启动并输出修复建议
- `agent-template preflight` 单独执行
- `agent-template doctor` 额外探测外部服务

#### API Key 管理
- 支持数据库动态 Key（不仅限于配置文件）
- CLI 命令：`apikey create/list/revoke`
- 支持过期时间和吊销
- 生产环境安全检查

#### 限流与配额
- GCRA 限流算法
- 按租户统计月度 token 配额
- 支持 `Idempotency-Key` 请求幂等
- Redis 故障时可选放行或拒绝

#### 错误码目录
- 集中定义 89 个错误码常量
- 统一错误响应格式
- 完整的错误码文档

#### Prompt 管理增强
- 支持 YAML front matter（用途、变量、推荐模型）
- 版本基于内容哈希生成
- 自动写入运行记录和 trace

#### 可观测性
- LLM 链路追踪（OpenTelemetry + GenAI 语义约定）
- 错误追踪（Sentry 集成）
- 23 个 Prometheus 指标
- 3 条告警规则
- Grafana 仪表板

#### 配置管理
- `agent-template config show` 脱敏显示配置
- `agent-template config validate` 验证配置
- 环境变量覆盖支持

### 改进

#### 性能优化
- Checkpointer 使用连接池（取代单连接）
- fence 锁使用独立连接池
- 后台运行 Worker 常驻事件循环（取代每任务重建）
- 指标采集改为定时（取代每次请求查询数据库）

#### 安全加固
- 限流与 Redis 解耦（故障不影响认证）
- 日志脱敏（堆栈、配置、参数）
- 生产环境默认值检查
- RLS 启用检查

#### 代码质量
- 删除 5 个空包和死代码
- 统一错误处理
- 模块化架构
- 类型完整性

### 文档

- 新增 `AGENTS.md` 开发指引
- 更新 `README.md` 添加代码示例
- 完善启动说明和配置概览
- 一键启动 Makefile

### 修复

- token 计数不再使用 JSON 字节数
- 就绪探针不再检查迁移版本精确匹配
- 运行超时正确归类为 `run_timeout`
- 流式输出不再每个 token 写一次数据库

---

## [0.2.0] - 2026-10-11

### 新增
- 初始版本
- 多框架支持（LangChain、LangGraph、LlamaIndex）
- 基础模型接入
- 工具治理
- 后台运行
- 知识库
- 长期记忆

---

## 迁移指南

### 从 0.2.0 迁移到 0.3.0

#### 1. 备份数据
```bash
# 导出重要数据
pg_dump -h localhost -p 5433 -U postgres agent_template > backup.sql
```

#### 2. 更新配置文件
参照上面的配置结构变更调整 `config/*.yaml`。

#### 3. 更新 Agent 代码
将所有 Agent 从 `AgentRegistration` 改为 `AgentSpec`，放在 `src/app/agents/*/agent.py` 并导出 `spec`。

#### 4. 删除 LlamaIndex Agent 运行时
如果使用了 LlamaIndex Workflow，需改为 LangGraph 或 LangChain `create_agent`。

#### 5. 重建数据库
```bash
# 删除旧数据库
docker compose down -v

# 重新初始化
make setup PRESET=minimal  # 或 full
```

#### 6. 更新 API 调用
调整客户端代码使用新的 API 端点。

#### 7. 更新 Compose 文件引用
```bash
# 旧方式
docker-compose up

# 新方式
docker compose --env-file .data/compose.env up -d

# full 档
docker compose -f compose.yaml -f compose.full.yaml --env-file .data/compose.env up -d
```

#### 8. 验证
```bash
make preflight
make check
```

---

[0.3.0]: https://github.com/twiink/agent-template/compare/v0.2.0...v0.3.0
[0.2.0]: https://github.com/twiink/agent-template/releases/tag/v0.2.0
