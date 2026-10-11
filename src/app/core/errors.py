"""统一可公开的错误协议；内部异常不直接暴露连接串、正文或服务商响应。"""

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException


class AppError(Exception):
    """可公开业务错误；code 稳定，retryable 只表示调用者能否重试。"""

    def __init__(self, code: str, message: str, status: int = 400, retryable: bool = False):
        super().__init__(message)
        self.code, self.message, self.status, self.retryable = code, message, status, retryable


def register_exception_handlers(app: FastAPI) -> None:
    """安装所有异常处理器；未分类异常保留请求 ID，返回固定信息避免泄露内部细节。"""

    def reply(request: Request, code: str, message: str, status: int, retryable: bool = False):
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
    async def app_error(request: Request, error: AppError):
        """把明确可公开的错误转为统一响应。"""
        return reply(request, error.code, error.message, error.status, error.retryable)

    @app.exception_handler(RequestValidationError)
    async def validation_error(request: Request, error: RequestValidationError):
        """仅公开字段位置，不回显未经验证的请求值。"""
        fields = ", ".join(".".join(map(str, e["loc"])) for e in error.errors())
        return reply(request, "validation_error", f"参数校验失败：{fields}", 422)

    @app.exception_handler(HTTPException)
    async def http_error(request: Request, error: HTTPException):
        """规范框架生成的 HTTP 错误。"""
        return reply(request, "http_error", str(error.detail), error.status_code)

    @app.exception_handler(Exception)
    async def unexpected_error(request: Request, error: Exception):
        """只记录异常类别；异常文本可能包含秘密，交由显式脱敏诊断处理。"""
        app.state.logger.error("请求处理失败", exception_type=type(error).__name__)
        return reply(request, "internal_error", "请求处理失败", 500)
