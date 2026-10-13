# AgentTemplate 开发指引

Python 3.12 + FastAPI + PostgreSQL + Redis 通用 Agent 后端模板。核心提供配置、启动预检、鉴权、限流、模型接入、工具治理、线程与运行管理；附件、后台运行、知识库和长期记忆做成可开关模块。

## 两档预设

**minimal**：只依赖 PostgreSQL，适合快速原型和简单场景。核心功能 + 同步执行 + 本地附件。

**full**：完整生产能力。在 minimal 基础上增加：后台运行、Worker、租约恢复、断线续传、RAG 知识库、长期记忆、观测部署文件。

预设决定默认启用的模块，之后可直接修改配置中的 `modules` 列表增减。

## 目录地图

```
src/app/
├── core/          配置、预检、日志、错误、鉴权、限流、指标、容器
├── db/            会话、核心表、仓储、迁移
├── api/           依赖、中间件、v1 路由
├── services/      线程、运行、健康检查、评测
├── llm/           模型网关、价格表、用量、token 计数
├── prompts/       Prompt 加载器
├── tools/         治理、内置工具（files、http、search、sql）、MCP
├── runtime/       执行器、事件、检查点、中间件、上下文
├── agents/        Agent 注册契约与自动发现（业务 Agent 放这里）
├── modules/       可选模块：storage、background、knowledge、memory
└── cli/           agent-template 命令

prompts/           Prompt 模板文件（业务 Prompt 放这里）
evals/             评测约定与评分器
config/            base、presets、环境配置、pricing
deploy/            kubernetes、observability
```

## 常用命令

```bash
# 初始化与启动
uv run agent-template init --preset minimal  # 或 full
uv sync                                      # minimal；full 用 uv sync --extra full
docker compose --env-file .data/compose.env up -d postgres redis
uv run agent-template migrate
uv run agent-template preflight
uv run agent-template dev

# full 预设追加
uv run agent-template worker
uv run agent-template scheduler

# 检查与诊断
uv run agent-template check       # 格式、lint、类型、规范
uv run agent-template preflight   # 启动预检
uv run agent-template doctor      # 预检 + 主动探测外部服务

# API Key 管理（full 档另有数据库动态 Key）
uv run agent-template apikey create <tenant> <subject>
uv run agent-template apikey list
uv run agent-template apikey revoke <key_id>

# 配置
uv run agent-template config show      # 脱敏后显示生效配置
uv run agent-template config validate  # 验证配置正确性
```

## 业务代码放哪里

| 要做的事 | 位置 |
|---|---|
| 新增 Agent | `src/app/agents/<名称>/agent.py`，导出 `spec: AgentSpec` |
| 新增工具 | 通用工具放 `src/app/tools/`；Agent 专用工具放该 Agent 目录 |
| 新增 Prompt | `prompts/<agent>/<名称>.md` |
| 新增业务表 | 模型放所属模块；迁移放对应 `migrations/`；配置 RLS 和授权 |
| 评测 | 数据集放 `evals/datasets/`，评分器放 `evals/scorers/` |

## 编码约定

- 注释与 docstring 使用中文。
- 公共接口说明用途、边界和副作用。
- 复杂逻辑解释设计理由。
- 项目文本及命令输出禁止 Emoji。
- 遵循 ruff、ty 检查规则。
- 敏感配置使用 `SecretStr`；配置文件不得提交凭据。

## 安全红线

- 密钥、密码、token 只存在受保护的配置或环境变量中。
- 日志、trace、错误消息脱敏，不回显秘密值。
- 工具调用必须通过 `@governed_tool` 治理。
- 写操作默认需要人工审批并记录副作用账本。
- 路径逐级验证，拒绝符号链接。
- HTTP 请求逐跳检查域名与实际 IP。
- SQL 工具使用只读事务与无特权角色。
- 身份只能来自已验证凭据，客户端和模型都不能覆盖。

## 扩展文档

- [开发指南](docs/开发指南.md)：配置、模块、Agent 注册、运行链路、治理分工
- [架构设计](docs/架构设计.md)：两档能力、目录结构、模块机制
- [配置说明](docs/配置说明.md)：配置分层、环境变量、模型别名
- [API 文档](docs/API文档.md)：接口规范、错误码目录
- [运维指南](docs/运维指南.md)：Compose、Kubernetes、迁移、监控
- [Agent 创建指南](docs/创建Agent指南.md)、[工具创建指南](docs/创建工具指南.md)和[Prompt 创建指南](docs/创建Prompt指南.md)

API 文档位于 `/docs`，资源接口在 `/api/v1`，必须携带 `Authorization: Bearer <凭据>`。
