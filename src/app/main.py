"""FastAPI 应用工厂：启动预检通过后在 lifespan 中建立资源容器，按启用的模块追加路由。

导入不产生外部连接；模块路由在创建应用时按配置决定，资源在 lifespan 中建立并逆序关闭。
"""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor

from app import __version__
from app.api import health
from app.api import v1 as api_v1
from app.api.middleware import install_request_context
from app.core.config import load_settings
from app.core.container import Container
from app.core.errors import register_exception_handlers
from app.core.logging import setup_logging
from app.core.preflight import require_preflight
from app.core.security import Authenticator
from app.modules import load, resolve
from app.services.health import ReadinessCache, run_metrics_collector


def create_app(config_override=None) -> FastAPI:
    """以显式覆盖生成独立应用；嵌入式调用不会共享全局 Settings。"""
    settings = load_settings(config_override)
    logger = setup_logging(settings)
    modules = [load(info) for info in resolve(settings.modules)]

    @asynccontextmanager
    async def lifespan(application):
        """先执行启动预检，再建立资源与后台指标采集；预检失败时应用不会开始接收请求。"""
        if settings.preflight.enabled:
            await require_preflight(settings, logger)
        async with Container(settings) as container:
            application.state.container = container
            collector = asyncio.create_task(run_metrics_collector(container))
            try:
                yield
            finally:
                collector.cancel()
                await asyncio.gather(collector, return_exceptions=True)

    application = FastAPI(title=settings.app_name, version=__version__, lifespan=lifespan)
    application.state.settings = settings
    application.state.authenticator = Authenticator(settings.auth)
    application.state.logger = logger
    application.state.readiness = ReadinessCache()
    application.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origins,
        allow_credentials=False,
        allow_methods=["GET", "POST", "PUT", "DELETE"],
        allow_headers=["Authorization", "Content-Type", "Last-Event-ID", "X-Request-ID"],
    )
    register_exception_handlers(application)
    install_request_context(application, logger)
    application.include_router(health.router)
    application.include_router(api_v1.router)
    for module in modules:
        if module.router:
            application.include_router(module.router(), prefix="/api/v1")

    if settings.tracing.enabled:
        FastAPIInstrumentor.instrument_app(application, excluded_urls="health/.*,metrics")
    return application


app = create_app()
