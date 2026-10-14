"""工具声明级治理：执行前再次核验可信运行身份及其作用域。"""

from __future__ import annotations

from collections.abc import Callable, Coroutine, Sequence
from functools import wraps
from typing import Any, ParamSpec, TypeVar

from app.core.errors import AppError, Code
from app.runtime.contracts import current_run

P = ParamSpec("P")
R = TypeVar("R")


def governed_tool(
    *, scopes: Sequence[str]
) -> Callable[[Callable[P, Coroutine[Any, Any, R]]], Callable[P, Coroutine[Any, Any, R]]]:
    """要求工具从可信运行上下文调用，并逐项验证其授权作用域。"""
    required_scopes = tuple(scopes)

    def decorate(
        function: Callable[P, Coroutine[Any, Any, R]],
    ) -> Callable[P, Coroutine[Any, Any, R]]:
        """为函数增加可信运行上下文和 scope 检查。"""

        @wraps(function)
        async def wrapped(*args: P.args, **kwargs: P.kwargs) -> R:
            """拒绝无运行身份或缺少所需权限的调用。"""
            context = current_run.get()
            if context is None:
                raise AppError(Code.RUN_CONTEXT_REQUIRED, "工具必须在可信运行上下文中调用", 403)
            for scope in required_scopes:
                context.principal.require(scope)
            return await function(*args, **kwargs)

        return wrapped

    return decorate
