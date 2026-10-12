"""请求上下文中间件：请求 ID、访问日志与 HTTP 指标。"""

from __future__ import annotations

import re
import time
from typing import Any
from uuid import uuid4

import structlog
from fastapi import FastAPI, Request

from app.core.telemetry import latency, requests

# 上游请求 ID 只接受常见字符和有限长度，避免日志注入和超长字段。
REQUEST_ID = re.compile(r"^[A-Za-z0-9._:-]{1,128}$")


def install_request_context(application: FastAPI, logger: Any) -> None:
    """注册请求上下文中间件；请求 ID 写入日志上下文和响应头，指标按路由模板统计。"""

    @application.middleware("http")
    async def correlation(request: Request, call_next):
        """沿用合法的上游请求 ID，否则生成新的；ID 写入日志上下文，指标按路由模板统计。"""
        incoming = request.headers.get("x-request-id", "")
        request.state.request_id = incoming if REQUEST_ID.match(incoming) else str(uuid4())
        # 每个请求在独立任务中执行，这里绑定的字段会带到本请求产生的全部日志里。
        structlog.contextvars.clear_contextvars()
        structlog.contextvars.bind_contextvars(request_id=request.state.request_id)
        started = time.monotonic()
        status = 500
        try:
            response = await call_next(request)
            status = response.status_code
            response.headers["X-Request-ID"] = request.state.request_id
            return response
        finally:
            # 未处理异常同样计入指标和访问日志；异常本身由统一异常处理器记录堆栈。
            route = getattr(request.scope.get("route"), "path", "unmatched")
            elapsed = time.monotonic() - started
            requests.labels(request.method, route, str(status)).inc()
            latency.labels(request.method, route).observe(elapsed)
            principal = getattr(request.state, "principal", None)
            logger.info(
                "接口请求完成",
                method=request.method,
                route=route,
                status=status,
                duration_ms=round(elapsed * 1000, 1),
                tenant_id=principal.tenant_id if principal else None,
                subject_id=principal.subject_id if principal else None,
            )
