# 验收清单

模板不包含测试代码（见 [重构计划](redesign-plan.md) 决策 D3）。模板自身的验收由静态检查、启动预检和冒烟检查组成；派生项目的业务测试在开发阶段按需添加。

验收结果只记录实际执行过的项目；依赖外部服务而未执行的项目，写明原因，不记为通过。

## 1. 静态检查

```bash
uv sync --extra full
uv run agent-template check
uv lock --check
```

`check` 依次执行 ruff 格式检查、ruff lint、ty 类型检查和中文说明与 Emoji 规范检查，任意一项失败即返回非零。类型检查需要 full 的依赖才能解析各模块的导入。

## 2. 启动预检

```bash
uv run agent-template preflight
uv run agent-template preflight --json
```

预检项与级别：

| 检查 | 内容 | 失败时 |
|---|---|---|
| 配置 | 配置来源、私有配置文件权限、是否配置聊天模型 | 配置校验失败时直接退出；权限与模型只警告 |
| 模块 | 启用的模块存在、依赖模块已启用、代码未被删除、所需 extras 已安装 | 阻断 |
| 连接配置 | 数据库与 Redis 连接不能停留在内置默认值 | 生产环境阻断，其他环境警告 |
| 目录 | 工具工作目录、本地附件目录可写；Prompt 目录存在 | 目录不可写时阻断，Prompt 目录缺失只警告 |
| PostgreSQL | 能否连通、服务端版本 | 阻断 |
| 数据库角色 | 运行连接不是超级用户，也没有 BYPASSRLS | 阻断 |
| 迁移 | 数据库版本属于本代码的版本链，核心与每个启用模块的分支都已升级到最新 | 阻断；`preflight.pending_migrations` 设为 `warn` 时未执行迁移只警告 |
| 行级安全 | 核心表与已启用模块的表均启用强制行级安全 | 阻断 |
| Redis | 能否连通 | 阻断 |

API、Worker 和 scheduler 启动时自动执行同样的检查；存在失败的阻断项时拒绝启动，并在日志中逐项写出原因和修复建议。只在特殊排障场景下才设置 `preflight.enabled=false` 跳过启动预检。

## 3. 冒烟检查

两档各做一遍，minimal 用 `--preset minimal` 和 `uv sync`，full 用 `--preset full` 和 `uv sync --extra full`：

```bash
uv run agent-template init --preset minimal
uv sync
docker compose --env-file .data/compose.env up -d postgres redis
uv run agent-template migrate
uv run agent-template preflight
uv run agent-template dev
```

服务启动后确认：

- `GET /health/live` 返回 200。
- `GET /health/ready` 返回 200；停止 PostgreSQL 或 Redis 后返回 503，恢复后重新变为 200。
- `GET /metrics` 返回指标；停止 PostgreSQL 后仍返回 200。
- 携带 `.data/developer.key` 中的凭据访问 `GET /api/v1/agents` 返回 200，响应头包含 `X-Request-ID`。
- 停止 Redis 后，`rate_limit_failure_mode=open` 时资源接口仍可访问，`closed` 时返回 503 和错误码 `rate_limit_unavailable`。
- 人为触发一次未处理异常，日志中包含 request_id 和已脱敏的异常堆栈。
- minimal 档 OpenAPI 文档中只有核心接口与附件接口；full 档另有后台运行、知识库与记忆接口。
- 在仓库副本中执行 `init --preset minimal --prune`，未启用模块的目录与 full 专用部署文件被删除后，应用仍能启动。

## 4. 依赖外部服务的验收

以下项目需要有效的服务商凭据或外部环境，未提供时在验收结果中注明未执行：

- 真实模型、Embedding、Tavily、MCP、S3 与观测后端的连通性和调用。
- 模型 usage、费用核算与重试。
- 知识导入、检索与索引重建。
- Kubernetes 集群中的迁移 Job、探针与滚动发布。
