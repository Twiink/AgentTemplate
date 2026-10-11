"""FastAPI 应用工厂；共享资源在 lifespan 中建立并逆序关闭，导入不产生外部连接。"""

from __future__ import annotations

import time
from contextlib import asynccontextmanager
from uuid import uuid4

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, Response
from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor
from prometheus_client import CONTENT_TYPE_LATEST, CollectorRegistry, generate_latest, multiprocess

from app.api.routes import router
from app.core.config import load_settings
from app.core.errors import register_exception_handlers
from app.core.observability import latency, requests, setup_logging
from app.core.resources import Resources
from app.core.security import Authenticator, current_principal
from app.services.health import diagnostics


def create_app(config_override=None) -> FastAPI:
    """以显式覆盖生成独立应用；测试与嵌入式调用不会共享全局 Settings。"""
    settings = load_settings(config_override)

    @asynccontextmanager
    async def lifespan(application):
        """基础连接可延迟失败，由 ready 报告；初始化异常同样释放部分成功的资源。"""
        resources = Resources(settings)
        try:
            async with resources:
                application.state.resources = resources
                yield
        finally:
            await resources.stack.aclose()

    application = FastAPI(title=settings.app_name, version="0.2.0", lifespan=lifespan)
    application.state.settings = settings
    application.state.authenticator = Authenticator(settings.auth)
    application.state.logger = setup_logging(settings)
    application.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origins,
        allow_credentials=False,
        allow_methods=["GET", "POST", "PUT", "DELETE"],
        allow_headers=["Authorization", "Content-Type", "Last-Event-ID"],
    )
    register_exception_handlers(application)
    application.include_router(router)

    @application.middleware("http")
    async def correlation(request: Request, call_next):
        """服务器生成请求 ID，按路由模板统计指标，避免原始路径造成高基数。"""
        request.state.request_id = str(uuid4())
        started = time.monotonic()
        response = await call_next(request)
        route = getattr(request.scope.get("route"), "path", "unmatched")
        requests.labels(request.method, route, str(response.status_code)).inc()
        latency.labels(request.method, route).observe(time.monotonic() - started)
        response.headers["X-Request-ID"] = request.state.request_id
        application.state.logger.info(
            "接口请求完成",
            request_id=request.state.request_id,
            method=request.method,
            route=route,
            status=response.status_code,
        )
        return response

    @application.get("/health/live")
    async def live() -> dict:
        """存活探针只确认进程能接受请求。"""
        return {"status": "alive"}

    @application.get("/health/ready")
    async def ready() -> Response:
        """数据库迁移、非特权连接和 Redis 正常才就绪，否则返回 503。"""
        result = await diagnostics(application.state.resources)
        return JSONResponse(result, status_code=200 if result["ready"] else 503)

    @application.get("/health/details")
    async def details(request: Request) -> dict:
        """详细诊断必须认证，避免公开依赖拓扑或配置来源。"""
        principal = await current_principal(request)
        principal.require("resources:read")
        return await diagnostics(application.state.resources, detailed=True)

    @application.get("/metrics")
    async def metrics() -> Response:
        """供受部署网络策略限制的 Prometheus 采集，标签不包含用户和线程。"""
        import os

        from sqlalchemy import func, select

        from app.core.observability import active_runs, queue_depth
        from app.db.models import Run

        async with application.state.resources.db.transaction(system=True) as session:
            queue_depth.set(
                (
                    await session.scalar(
                        select(func.count()).select_from(Run).where(Run.status == "queued")
                    )
                )
                or 0
            )
            active_runs.set(
                (
                    await session.scalar(
                        select(func.count()).select_from(Run).where(Run.status == "running")
                    )
                )
                or 0
            )
        if os.getenv("PROMETHEUS_MULTIPROC_DIR"):
            registry = CollectorRegistry()
            multiprocess.MultiProcessCollector(registry)
            content = generate_latest(registry)
        else:
            content = generate_latest()
        return Response(content, headers={"Content-Type": CONTENT_TYPE_LATEST})

    if settings.tracing.enabled:
        FastAPIInstrumentor.instrument_app(application, excluded_urls="health/.*,metrics")
    return application


app = create_app()
