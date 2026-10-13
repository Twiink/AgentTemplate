# API 参考

## 错误码

模板使用统一的错误响应结构，`error.code` 字段是稳定的错误码：

```json
{
  "error": {
    "code": "resource_not_found",
    "message": "运行不存在",
    "request_id": "req_abc123",
    "retryable": false
  }
}
```

### 错误码目录

所有错误码在 `src/app/core/errors.py` 的 `Code` 类上集中定义，下表列出每个错误码的默认 HTTP 状态、是否可重试及中文说明：

| 错误码 | HTTP 状态 | 可重试 | 说明 |
|---|---|---|---|
| `unauthorized` | 401 | 否 | 缺少凭据或凭据无效 |
| `forbidden` | 403 | 否 | 身份有效但权限不足 |
| `resource_not_found` | 404 | 否 | 资源不存在或不属于当前租户 |
| `validation_error` | 422 | 否 | 请求参数校验失败 |
| `internal_error` | 500 | 否 | 未分类的服务端异常 |
| `module_unavailable` | 503 | 否 | 依赖的模块未启用或未装配 |
| `reserved_configuration` | 400 | 否 | 配置触碰了模板保留的设置项 |
| `checkpointer_required` | 400 | 否 | 该操作需要 Checkpointer |
| `runtime_role_missing` | 400 | 否 | 运行连接未使用专门角色 |
| `legacy_database` | 400 | 否 | 数据库版本不属于本代码的迁移版本链 |
| `async_required` | 400 | 否 | 需要在异步上下文中调用 |
| `idempotency_conflict` | 409 | 否 | 同一幂等键对应了不同的请求 |
| `rate_limited` | 429 | 是 | 超过每分钟请求上限 |
| `rate_limit_unavailable` | 503 | 是 | 限流后端不可用且策略为拒绝 |
| `token_quota_exhausted` | 429 | 否 | 按租户统计的每日 Token 配额已用完 |
| `model_not_configured` | 503 | 否 | 引用的模型别名未配置或无可用凭据 |
| `pricing_required` | 503 | 否 | 价格表未收录该模型，无法计算费用 |
| `pricing_version_mismatch` | 503 | 否 | 价格表版本与模型声明不一致 |
| `context_budget` | 400 | 否 | 输入超出上下文预算 |
| `cost_budget` | 400 | 否 | 运行超出费用预算 |
| `token_budget` | 400 | 否 | 运行超出 Token 预算 |
| `unsupported_capability` | 400 | 否 | 模型未声明请求所需的能力 |
| `embedding_configuration` | 503 | 否 | Embedding 别名或维度未配置 |
| `embedding_dimensions` | 400 | 否 | 向量维度与既有空间不一致 |
| `embedding_profile_changed` | 400 | 否 | Embedding 快照已更换，需要重建索引 |
| `agent_not_found` | 404 | 否 | Agent ID 未注册 |
| `agent_not_registered` | 404 | 否 | 请求的 Agent 不在启用列表内 |
| `agent_input_invalid` | 422 | 否 | 输入不符合 Agent 声明的结构 |
| `agent_spec_invalid` | 400 | 否 | AgentSpec 声明不合法 |
| `agent_build_failed` | 400 | 否 | Agent 构建函数抛出异常 |
| `agent_id_duplicate` | 400 | 否 | 存在重复的 Agent ID |
| `run_context_required` | 403 | 否 | 缺少可信的运行上下文 |
| `run_cancelled` | 409 | 否 | 运行已被取消 |
| `run_timeout` | 400 | 否 | 运行超过时间预算 |
| `run_not_finished` | 409 | 否 | 运行尚未进入可结束状态 |
| `resume_conflict` | 409 | 否 | 恢复序号与当前暂停点不一致 |
| `lease_lost` | 409 | 否 | 租约已被其他消费者接管 |
| `thread_busy` | 409 | 否 | 同一线程已有运行在执行 |
| `invalid_event_id` | 422 | 否 | 事件序号无法解析 |
| `reconciliation_required` | 422 | 否 | 未知副作用调用需要业务核对结果 |
| `tool_not_found` | 404 | 否 | 工具未注册或未启用 |
| `tool_approval_denied` | 403 | 否 | 写工具未获得确认 |
| `tool_outcome_unknown` | 409 | 否 | 写工具的结果未知，需要人工核对 |
| `path_denied` | 403 | 否 | 路径越界或包含符号链接 |
| `file_access_denied` | 403 | 否 | 文件访问被拒绝 |
| `file_too_large` | 413 | 否 | 文件超过配置上限 |
| `http_target_denied` | 403 | 否 | 目标域名或连接 IP 不在允许范围 |
| `http_upstream_error` | 502 | 是 | 上游 HTTP 返回错误状态 |
| `http_response_too_large` | 400 | 否 | 响应超过配置上限 |
| `http_redirect_limit` | 400 | 否 | 重定向次数超过上限 |
| `search_not_configured` | 503 | 否 | 搜索服务未配置 |
| `search_upstream_error` | 502 | 是 | 搜索服务返回错误状态 |
| `sql_not_configured` | 503 | 否 | 只读 SQL 连接未配置 |
| `sql_denied` | 403 | 否 | SQL 语句未通过白名单校验 |
| `sql_table_denied` | 403 | 否 | 语句引用了未允许的表 |
| `sql_function_denied` | 403 | 否 | 语句引用了未允许的函数 |
| `sql_role_denied` | 403 | 否 | 只读连接使用的角色权限过高 |
| `mcp_not_configured` | 503 | 否 | MCP 服务未配置 |
| `mcp_configuration` | 503 | 否 | MCP 配置不完整或不可用 |
| `mcp_credential_missing` | 503 | 否 | MCP 凭据环境变量缺失 |
| `mcp_disconnected` | 503 | 是 | MCP 会话已断开 |
| `mcp_tool_error` | 502 | 否 | MCP 工具调用失败 |
| `storage_not_configured` | 503 | 否 | 附件后端未配置 |
| `artifact_missing` | 404 | 否 | 附件对象不存在 |
| `artifact_in_use` | 409 | 否 | 附件仍被引用，不能删除 |
| `artifact_too_large` | 413 | 否 | 附件超过配置上限 |
| `invalid_object_key` | 400 | 否 | 对象键不合法 |
| `knowledge_busy` | 409 | 否 | 知识库正在执行导入或重建 |
| `knowledge_rebuilding` | 409 | 否 | 索引正在重建 |
| `import_conflict` | 409 | 否 | 同一附件已存在导入任务 |
| `document_format` | 400 | 否 | 文档格式不受支持 |
| `document_empty` | 400 | 否 | 文档没有可提取的正文 |
| `document_parser` | 400 | 否 | 文档解析失败 |
| `ocr_required` | 400 | 否 | 扫描件没有可提取文字，需要注册 OCR |
| `chunk_configuration` | 400 | 否 | 切分参数不合法 |
| `reranker_not_configured` | 503 | 否 | 重排服务未配置 |
| `memory_source_missing` | 400 | 否 | 记忆来源引用不存在 |
| `memory_extractor_failed` | 400 | 否 | 记忆提取任务失败 |
| `prompt_not_found` | 404 | 否 | Prompt 不存在 |
| `prompt_parameters` | 400 | 否 | Prompt 参数与声明不匹配 |
| `feedback_configuration` | 400 | 否 | 反馈配置不完整 |
| `feedback_failed` | 502 | 否 | 反馈上报失败 |
| `dataset_not_found` | 404 | 否 | 评测数据集不存在 |
| `evaluation_identity` | 400 | 否 | 评测缺少必要的身份信息 |
| `evaluation_cases` | 400 | 否 | 评测用例为空或不合法 |
| `evaluation_baseline` | 400 | 否 | 基线文件不可用 |
| `evaluation_regression` | 400 | 否 | 评测结果低于基线阈值 |
| `evaluation_timeout` | 400 | 否 | 评测超时 |
| `evaluation_requires_background` | 400 | 否 | 该评测需要启用 background 模块 |
| `scorer_result` | 400 | 否 | 评分器返回值不合法 |
| `observability_targets` | 400 | 否 | 未选择观测后端 |
| `observability_exists` | 400 | 否 | 观测私有配置已存在 |
| `quota_exceeded` | 429 | 否 | 超过月度费用配额 |

**注意**：未显式登记的错误码按 HTTP 400 处理。完整的错误码定义见 `src/app/core/errors.py`。
| `memory_extractor_failed` | 400 | 否 | 记忆提取任务失败 |
| `prompt_not_found` | 404 | 否 | Prompt 不存在 |
| `prompt_parameters` | 400 | 否 | Prompt 参数与声明不匹配 |
| `feedback_configuration` | 400 | 否 | 反馈配置不完整 |
| `feedback_failed` | 502 | 否 | 反馈上报失败 |
| `dataset_not_found` | 404 | 否 | 评测数据集不存在 |
| `evaluation_identity` | 400 | 否 | 评测缺少必要的身份信息 |
| `evaluation_cases` | 400 | 否 | 评测用例为空或不合法 |
| `evaluation_baseline` | 400 | 否 | 基线文件不可用 |
| `evaluation_regression` | 400 | 否 | 评测结果低于基线阈值 |
| `evaluation_timeout` | 400 | 否 | 评测超时 |
| `evaluation_requires_background` | 400 | 否 | 该评测需要启用 background 模块 |
| `scorer_result` | 400 | 否 | 评分器返回值不合法 |
| `observability_targets` | 400 | 否 | 未选择观测后端 |
| `observability_exists` | 400 | 否 | 观测私有配置已存在 |

### 状态码说明

- **400 Bad Request**：请求本身有误（参数缺失、格式错误、预算超限等）
- **401 Unauthorized**：缺少有效凭据
- **403 Forbidden**：身份有效但权限不足（工具、路径、表访问被拒绝）
- **404 Not Found**：资源不存在
- **409 Conflict**：状态冲突（运行已取消、线程繁忙、租约丢失等）
- **413 Payload Too Large**：文件或附件超过配置上限
- **422 Unprocessable Entity**：语义校验失败（输入结构、参数不匹配）
- **429 Too Many Requests**：限流或配额耗尽
- **502 Bad Gateway**：上游服务返回错误
- **503 Service Unavailable**：依赖未配置或暂不可用

### 可重试错误

- `rate_limited`：客户端应遵守 `Retry-After` 响应头后重试
- `rate_limit_unavailable`：限流后端故障且策略为拒绝，可短暂后重试
- `http_upstream_error`：上游服务 5xx 错误，可重试
- `search_upstream_error`：搜索服务 5xx 错误，可重试
- `mcp_disconnected`：MCP 会话断开，可重连后重试

其他错误码均标记为不可重试（`retryable: false`）：重试不会改变结果，客户端应该修正请求或报告问题。

## 接口列表

完整 OpenAPI 规范见 `/docs`（开发环境）或 `/api/openapi.json`。

### 核心接口

- `POST /api/v1/threads` 创建线程
- `GET /api/v1/threads/{id}` 获取线程
- `POST /api/v1/threads/{id}/runs` 创建运行（流式或阻塞）
- `GET /api/v1/runs/{id}/stream` SSE 订阅运行事件
- `GET /api/v1/runs/{id}/result` 获取终态结果
- `POST /api/v1/runs/{id}/cancel` 取消运行

### 后台运行（full 预设）

- `POST /api/v1/threads/{id}/runs` 持久排队并返回 202

### 健康与指标

- `GET /health/live` 存活探针
- `GET /health/ready` 就绪探针
- `GET /health/details` 详细诊断（需要认证）
- `GET /metrics` Prometheus 指标

模块接口（storage、knowledge、memory）见各模块的 API 路由。
