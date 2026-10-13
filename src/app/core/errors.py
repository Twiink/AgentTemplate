"""统一可公开的错误协议与错误码目录；内部异常不直接暴露连接串、正文或服务商响应。

错误码集中在这里定义：代码中只引用 `Code` 上的常量，不再散落字符串字面量。
`CATALOG` 给出每个错误码的默认 HTTP 状态、是否可重试和中文说明，`AppError` 未显式
传状态时按目录取值；确有必要覆盖状态时，调用点仍可显式传 `status`。
"""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass
from typing import Any

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException


class Code:
    """错误码常量；取值即公开响应中的 `error.code`。

    只在这里新增或修改错误码，并同步在 `CATALOG` 登记状态与说明，否则启动时校验会失败。
    """

    # 认证与授权
    UNAUTHORIZED = "unauthorized"
    FORBIDDEN = "forbidden"

    # 通用资源
    RESOURCE_NOT_FOUND = "resource_not_found"
    VALIDATION_ERROR = "validation_error"
    HTTP_ERROR = "http_error"
    INTERNAL_ERROR = "internal_error"

    # 配置与模块可用性
    MODULE_UNAVAILABLE = "module_unavailable"
    RESERVED_CONFIGURATION = "reserved_configuration"
    CHECKPOINTER_REQUIRED = "checkpointer_required"
    RUNTIME_ROLE_MISSING = "runtime_role_missing"
    LEGACY_DATABASE = "legacy_database"
    ASYNC_REQUIRED = "async_required"
    IDEMPOTENCY_CONFLICT = "idempotency_conflict"

    # 限流与配额
    RATE_LIMITED = "rate_limited"
    RATE_LIMIT_UNAVAILABLE = "rate_limit_unavailable"
    QUOTA_EXCEEDED = "quota_exceeded"
    TOKEN_QUOTA_EXHAUSTED = "token_quota_exhausted"

    # 模型与向量
    MODEL_NOT_CONFIGURED = "model_not_configured"
    PRICING_REQUIRED = "pricing_required"
    PRICING_VERSION_MISMATCH = "pricing_version_mismatch"
    CONTEXT_BUDGET = "context_budget"
    COST_BUDGET = "cost_budget"
    TOKEN_BUDGET = "token_budget"
    UNSUPPORTED_CAPABILITY = "unsupported_capability"
    EMBEDDING_CONFIGURATION = "embedding_configuration"
    EMBEDDING_DIMENSIONS = "embedding_dimensions"
    EMBEDDING_PROFILE_CHANGED = "embedding_profile_changed"

    # Agent 与运行
    AGENT_NOT_FOUND = "agent_not_found"
    AGENT_NOT_REGISTERED = "agent_not_registered"
    AGENT_INPUT_INVALID = "agent_input_invalid"
    AGENT_SPEC_INVALID = "agent_spec_invalid"
    AGENT_BUILD_FAILED = "agent_build_failed"
    AGENT_ID_DUPLICATE = "agent_id_duplicate"
    RUN_CONTEXT_REQUIRED = "run_context_required"
    RUN_CANCELLED = "run_cancelled"
    RUN_TIMEOUT = "run_timeout"
    RUN_NOT_FINISHED = "run_not_finished"
    RESUME_CONFLICT = "resume_conflict"
    LEASE_LOST = "lease_lost"
    THREAD_BUSY = "thread_busy"
    INVALID_EVENT_ID = "invalid_event_id"
    RECONCILIATION_REQUIRED = "reconciliation_required"

    # 工具与副作用
    TOOL_NOT_FOUND = "tool_not_found"
    TOOL_APPROVAL_DENIED = "tool_approval_denied"
    TOOL_OUTCOME_UNKNOWN = "tool_outcome_unknown"
    PATH_DENIED = "path_denied"
    FILE_ACCESS_DENIED = "file_access_denied"
    FILE_TOO_LARGE = "file_too_large"
    HTTP_TARGET_DENIED = "http_target_denied"
    HTTP_UPSTREAM_ERROR = "http_upstream_error"
    HTTP_RESPONSE_TOO_LARGE = "http_response_too_large"
    HTTP_REDIRECT_LIMIT = "http_redirect_limit"
    SEARCH_NOT_CONFIGURED = "search_not_configured"
    SEARCH_UPSTREAM_ERROR = "search_upstream_error"
    SQL_NOT_CONFIGURED = "sql_not_configured"
    SQL_DENIED = "sql_denied"
    SQL_TABLE_DENIED = "sql_table_denied"
    SQL_FUNCTION_DENIED = "sql_function_denied"
    SQL_ROLE_DENIED = "sql_role_denied"
    MCP_NOT_CONFIGURED = "mcp_not_configured"
    MCP_CONFIGURATION = "mcp_configuration"
    MCP_CREDENTIAL_MISSING = "mcp_credential_missing"
    MCP_DISCONNECTED = "mcp_disconnected"
    MCP_TOOL_ERROR = "mcp_tool_error"

    # 存储
    STORAGE_NOT_CONFIGURED = "storage_not_configured"
    ARTIFACT_MISSING = "artifact_missing"
    ARTIFACT_IN_USE = "artifact_in_use"
    ARTIFACT_TOO_LARGE = "artifact_too_large"
    INVALID_OBJECT_KEY = "invalid_object_key"

    # 知识与文档
    KNOWLEDGE_BUSY = "knowledge_busy"
    KNOWLEDGE_REBUILDING = "knowledge_rebuilding"
    IMPORT_CONFLICT = "import_conflict"
    DOCUMENT_FORMAT = "document_format"
    DOCUMENT_EMPTY = "document_empty"
    DOCUMENT_PARSER = "document_parser"
    OCR_REQUIRED = "ocr_required"
    CHUNK_CONFIGURATION = "chunk_configuration"
    RERANKER_NOT_CONFIGURED = "reranker_not_configured"

    # 记忆
    MEMORY_SOURCE_MISSING = "memory_source_missing"
    MEMORY_EXTRACTOR_FAILED = "memory_extractor_failed"

    # Prompt 与反馈
    PROMPT_NOT_FOUND = "prompt_not_found"
    PROMPT_PARAMETERS = "prompt_parameters"
    FEEDBACK_CONFIGURATION = "feedback_configuration"
    FEEDBACK_FAILED = "feedback_failed"

    # 评测
    DATASET_NOT_FOUND = "dataset_not_found"
    EVALUATION_IDENTITY = "evaluation_identity"
    EVALUATION_CASES = "evaluation_cases"
    EVALUATION_BASELINE = "evaluation_baseline"
    EVALUATION_REGRESSION = "evaluation_regression"
    EVALUATION_TIMEOUT = "evaluation_timeout"
    EVALUATION_REQUIRES_BACKGROUND = "evaluation_requires_background"
    SCORER_RESULT = "scorer_result"

    # 运维命令
    OBSERVABILITY_TARGETS = "observability_targets"
    OBSERVABILITY_EXISTS = "observability_exists"


@dataclass(frozen=True)
class CodeInfo:
    """错误码元信息：默认状态、是否可重试、中文说明。"""

    status: int
    retryable: bool
    summary: str


# 只有需要覆盖默认状态或重试语义的错误码才显式登记；未登记的一律按 400 处理。
# 状态码的选择：400 请求本身有误，401/403 身份与权限，404 资源不存在，409 状态冲突，
# 413 体积超限，422 语义校验失败，429 限流，502 上游异常，503 依赖未配置或暂不可用。
_EXPLICIT: dict[str, CodeInfo] = {
    Code.UNAUTHORIZED: CodeInfo(401, False, "缺少凭据或凭据无效"),
    Code.FORBIDDEN: CodeInfo(403, False, "身份有效但权限不足"),
    Code.RESOURCE_NOT_FOUND: CodeInfo(404, False, "资源不存在或不属于当前租户"),
    Code.VALIDATION_ERROR: CodeInfo(422, False, "请求参数校验失败"),
    Code.INTERNAL_ERROR: CodeInfo(500, False, "未分类的服务端异常"),
    Code.MODULE_UNAVAILABLE: CodeInfo(503, False, "依赖的模块未启用或未装配"),
    Code.RESERVED_CONFIGURATION: CodeInfo(400, False, "配置触碰了模板保留的设置项"),
    Code.CHECKPOINTER_REQUIRED: CodeInfo(400, False, "该操作需要 Checkpointer"),
    Code.RUNTIME_ROLE_MISSING: CodeInfo(400, False, "运行连接未使用专门角色"),
    Code.LEGACY_DATABASE: CodeInfo(400, False, "数据库版本不属于本代码的迁移版本链"),
    Code.ASYNC_REQUIRED: CodeInfo(400, False, "需要在异步上下文中调用"),
    Code.IDEMPOTENCY_CONFLICT: CodeInfo(409, False, "同一幂等键对应了不同的请求"),
    Code.RATE_LIMITED: CodeInfo(429, True, "超过每分钟请求上限"),
    Code.RATE_LIMIT_UNAVAILABLE: CodeInfo(503, True, "限流后端不可用且策略为拒绝"),
    Code.QUOTA_EXCEEDED: CodeInfo(429, False, "超过月度费用配额"),
    Code.TOKEN_QUOTA_EXHAUSTED: CodeInfo(429, False, "按租户统计的每日 Token 配额已用完"),
    Code.MODEL_NOT_CONFIGURED: CodeInfo(503, False, "引用的模型别名未配置或无可用凭据"),
    Code.PRICING_REQUIRED: CodeInfo(503, False, "价格表未收录该模型，无法计算费用"),
    Code.PRICING_VERSION_MISMATCH: CodeInfo(503, False, "价格表版本与模型声明不一致"),
    Code.CONTEXT_BUDGET: CodeInfo(400, False, "输入超出上下文预算"),
    Code.COST_BUDGET: CodeInfo(400, False, "运行超出费用预算"),
    Code.TOKEN_BUDGET: CodeInfo(400, False, "运行超出 Token 预算"),
    Code.UNSUPPORTED_CAPABILITY: CodeInfo(400, False, "模型未声明请求所需的能力"),
    Code.EMBEDDING_CONFIGURATION: CodeInfo(503, False, "Embedding 别名或维度未配置"),
    Code.EMBEDDING_DIMENSIONS: CodeInfo(400, False, "向量维度与既有空间不一致"),
    Code.EMBEDDING_PROFILE_CHANGED: CodeInfo(400, False, "Embedding 快照已更换，需要重建索引"),
    Code.AGENT_NOT_FOUND: CodeInfo(404, False, "Agent ID 未注册"),
    Code.AGENT_NOT_REGISTERED: CodeInfo(404, False, "请求的 Agent 不在启用列表内"),
    Code.AGENT_INPUT_INVALID: CodeInfo(422, False, "输入不符合 Agent 声明的结构"),
    Code.AGENT_SPEC_INVALID: CodeInfo(400, False, "AgentSpec 声明不合法"),
    Code.AGENT_BUILD_FAILED: CodeInfo(400, False, "Agent 构建函数抛出异常"),
    Code.AGENT_ID_DUPLICATE: CodeInfo(400, False, "存在重复的 Agent ID"),
    Code.RUN_CONTEXT_REQUIRED: CodeInfo(403, False, "缺少可信的运行上下文"),
    Code.RUN_CANCELLED: CodeInfo(409, False, "运行已被取消"),
    Code.RUN_TIMEOUT: CodeInfo(400, False, "运行超过时间预算"),
    Code.RUN_NOT_FINISHED: CodeInfo(409, False, "运行尚未进入可结束状态"),
    Code.RESUME_CONFLICT: CodeInfo(409, False, "恢复序号与当前暂停点不一致"),
    Code.LEASE_LOST: CodeInfo(409, False, "租约已被其他消费者接管"),
    Code.THREAD_BUSY: CodeInfo(409, False, "同一线程已有运行在执行"),
    Code.INVALID_EVENT_ID: CodeInfo(422, False, "事件序号无法解析"),
    Code.RECONCILIATION_REQUIRED: CodeInfo(422, False, "未知副作用调用需要业务核对结果"),
    Code.TOOL_NOT_FOUND: CodeInfo(404, False, "工具未注册或未启用"),
    Code.TOOL_APPROVAL_DENIED: CodeInfo(403, False, "写工具未获得确认"),
    Code.TOOL_OUTCOME_UNKNOWN: CodeInfo(409, False, "写工具的结果未知，需要人工核对"),
    Code.PATH_DENIED: CodeInfo(403, False, "路径越界或包含符号链接"),
    Code.FILE_ACCESS_DENIED: CodeInfo(403, False, "文件访问被拒绝"),
    Code.FILE_TOO_LARGE: CodeInfo(413, False, "文件超过配置上限"),
    Code.HTTP_TARGET_DENIED: CodeInfo(403, False, "目标域名或连接 IP 不在允许范围"),
    Code.HTTP_UPSTREAM_ERROR: CodeInfo(502, True, "上游 HTTP 返回错误状态"),
    Code.HTTP_RESPONSE_TOO_LARGE: CodeInfo(400, False, "响应超过配置上限"),
    Code.HTTP_REDIRECT_LIMIT: CodeInfo(400, False, "重定向次数超过上限"),
    Code.SEARCH_NOT_CONFIGURED: CodeInfo(503, False, "搜索服务未配置"),
    Code.SEARCH_UPSTREAM_ERROR: CodeInfo(502, True, "搜索服务返回错误状态"),
    Code.SQL_NOT_CONFIGURED: CodeInfo(503, False, "只读 SQL 连接未配置"),
    Code.SQL_DENIED: CodeInfo(403, False, "SQL 语句未通过白名单校验"),
    Code.SQL_TABLE_DENIED: CodeInfo(403, False, "语句引用了未允许的表"),
    Code.SQL_FUNCTION_DENIED: CodeInfo(403, False, "语句引用了未允许的函数"),
    Code.SQL_ROLE_DENIED: CodeInfo(403, False, "只读连接使用的角色权限过高"),
    Code.MCP_NOT_CONFIGURED: CodeInfo(503, False, "MCP 服务未配置"),
    Code.MCP_CONFIGURATION: CodeInfo(503, False, "MCP 配置不完整或不可用"),
    Code.MCP_CREDENTIAL_MISSING: CodeInfo(503, False, "MCP 凭据环境变量缺失"),
    Code.MCP_DISCONNECTED: CodeInfo(503, True, "MCP 会话已断开"),
    Code.MCP_TOOL_ERROR: CodeInfo(502, False, "MCP 工具调用失败"),
    Code.STORAGE_NOT_CONFIGURED: CodeInfo(503, False, "附件后端未配置"),
    Code.ARTIFACT_MISSING: CodeInfo(404, False, "附件对象不存在"),
    Code.ARTIFACT_IN_USE: CodeInfo(409, False, "附件仍被引用，不能删除"),
    Code.ARTIFACT_TOO_LARGE: CodeInfo(413, False, "附件超过配置上限"),
    Code.INVALID_OBJECT_KEY: CodeInfo(400, False, "对象键不合法"),
    Code.KNOWLEDGE_BUSY: CodeInfo(409, False, "知识库正在执行导入或重建"),
    Code.KNOWLEDGE_REBUILDING: CodeInfo(409, False, "索引正在重建"),
    Code.IMPORT_CONFLICT: CodeInfo(409, False, "同一附件已存在导入任务"),
    Code.DOCUMENT_FORMAT: CodeInfo(400, False, "文档格式不受支持"),
    Code.DOCUMENT_EMPTY: CodeInfo(400, False, "文档没有可提取的正文"),
    Code.DOCUMENT_PARSER: CodeInfo(400, False, "文档解析失败"),
    Code.OCR_REQUIRED: CodeInfo(400, False, "扫描件没有可提取文字，需要注册 OCR"),
    Code.CHUNK_CONFIGURATION: CodeInfo(400, False, "切分参数不合法"),
    Code.RERANKER_NOT_CONFIGURED: CodeInfo(503, False, "重排服务未配置"),
    Code.MEMORY_SOURCE_MISSING: CodeInfo(400, False, "记忆来源引用不存在"),
    Code.MEMORY_EXTRACTOR_FAILED: CodeInfo(400, False, "记忆提取任务失败"),
    Code.PROMPT_NOT_FOUND: CodeInfo(404, False, "Prompt 不存在"),
    Code.PROMPT_PARAMETERS: CodeInfo(400, False, "Prompt 参数与声明不匹配"),
    Code.FEEDBACK_CONFIGURATION: CodeInfo(400, False, "反馈配置不完整"),
    Code.FEEDBACK_FAILED: CodeInfo(502, False, "反馈上报失败"),
    Code.DATASET_NOT_FOUND: CodeInfo(404, False, "评测数据集不存在"),
    Code.EVALUATION_IDENTITY: CodeInfo(400, False, "评测缺少必要的身份信息"),
    Code.EVALUATION_CASES: CodeInfo(400, False, "评测用例为空或不合法"),
    Code.EVALUATION_BASELINE: CodeInfo(400, False, "基线文件不可用"),
    Code.EVALUATION_REGRESSION: CodeInfo(400, False, "评测结果低于基线阈值"),
    Code.EVALUATION_TIMEOUT: CodeInfo(400, False, "评测超时"),
    Code.EVALUATION_REQUIRES_BACKGROUND: CodeInfo(400, False, "该评测需要启用 background 模块"),
    Code.SCORER_RESULT: CodeInfo(400, False, "评分器返回值不合法"),
    Code.OBSERVABILITY_TARGETS: CodeInfo(400, False, "未选择观测后端"),
    Code.OBSERVABILITY_EXISTS: CodeInfo(400, False, "观测私有配置已存在"),
}

# 未显式登记的错误码按 400 处理；多数业务校验失败属于这一类。
DEFAULT_STATUS = 400
DEFAULT_RETRYABLE = False


def _build_catalog() -> dict[str, CodeInfo]:
    """从 `Code` 上的常量生成完整目录；缺少登记的常量按默认值补齐。"""
    catalog: dict[str, CodeInfo] = {}
    for name, value in vars(Code).items():
        if name.startswith("_") or not isinstance(value, str):
            continue
        catalog[value] = _EXPLICIT.get(
            value, CodeInfo(DEFAULT_STATUS, DEFAULT_RETRYABLE, "业务校验失败")
        )
    return catalog


CATALOG: dict[str, CodeInfo] = _build_catalog()


def catalog_entries() -> Iterator[tuple[str, CodeInfo]]:
    """按错误码排序输出目录，供文档与自检使用。"""
    for code in sorted(CATALOG):
        yield code, CATALOG[code]


class AppError(Exception):
    """可公开业务错误；code 稳定，retryable 只表示调用者能否重试。

    `status` 与 `retryable` 缺省时取 `CATALOG` 中该错误码的取值；确需覆盖时显式传入。
    """

    def __init__(
        self,
        code: str,
        message: str,
        status: int | None = None,
        retryable: bool | None = None,
    ):
        super().__init__(message)
        # 未登记的错误码按默认状态处理，不阻断请求；集中定义由 5.5 的目录自检兜住。
        info = CATALOG.get(code)
        self.code = code
        self.message = message
        self.status = status if status is not None else (info.status if info else DEFAULT_STATUS)
        self.retryable = (
            retryable if retryable is not None else (info.retryable if info else DEFAULT_RETRYABLE)
        )


def register_exception_handlers(app: FastAPI) -> None:
    """安装所有异常处理器；未分类异常保留请求 ID，返回固定信息避免泄露内部细节。"""

    def reply(
        request: Request, code: str, message: str, status: int, retryable: bool = False
    ) -> JSONResponse:
        """构造统一错误结构；公开内容必须已经脱敏。"""
        return JSONResponse(
            status_code=status,
            content={
                "error": {
                    "code": code,
                    "message": message,
                    "request_id": getattr(request.state, "request_id", None),
                    "retryable": retryable,
                }
            },
        )

    @app.exception_handler(AppError)
    async def app_error(request: Request, error: AppError) -> JSONResponse:
        """把明确可公开的错误转为统一响应。"""
        return reply(request, error.code, error.message, error.status, error.retryable)

    @app.exception_handler(RequestValidationError)
    async def validation_error(request: Request, error: RequestValidationError) -> JSONResponse:
        """仅公开字段位置，不回显未经验证的请求值。"""
        fields = ", ".join(".".join(map(str, e["loc"])) for e in error.errors())
        return reply(request, Code.VALIDATION_ERROR, f"参数校验失败：{fields}", 422)

    @app.exception_handler(HTTPException)
    async def http_error(request: Request, error: HTTPException) -> JSONResponse:
        """规范框架生成的 HTTP 错误。"""
        return reply(request, Code.HTTP_ERROR, str(error.detail), error.status_code)

    @app.exception_handler(Exception)
    async def unexpected_error(request: Request, error: Exception) -> JSONResponse:
        """日志记录完整堆栈，经脱敏处理器后写出；响应只返回固定信息和请求 ID。"""
        app.state.logger.error("请求处理失败", exception_type=type(error).__name__, exc_info=error)
        return reply(request, Code.INTERNAL_ERROR, "请求处理失败", 500)


def code_constants() -> dict[str, str]:
    """常量名到错误码的映射；用于自检与文档生成。"""
    return {
        name: value
        for name, value in vars(Code).items()
        if not name.startswith("_") and isinstance(value, str)
    }


def unused_codes(used: set[str]) -> set[str]:
    """已在 `Code` 登记但代码中未引用的错误码；供自检提示清理。"""
    return set(CATALOG) - used


def missing_codes(values: Any) -> set[str]:
    """调用点使用但未在 `Code` 中登记的错误码。"""
    return {value for value in values if value not in CATALOG}
