"""健康检查与指标端点：存活、就绪、需要认证的详细诊断，以及 Prometheus 指标。"""

from __future__ import annotations

import os

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, Response
from prometheus_client import CONTENT_TYPE_LATEST, CollectorRegistry, generate_latest, multiprocess

from app.core.security import current_principal
from app.services.health import diagnostics

router = APIRouter(tags=["health"])


@router.get("/health/live")
async def live() -> dict:
    """存活探针只确认进程能接受请求。"""
    return {"status": "alive"}


@router.get("/health/ready")
async def ready(request: Request) -> Response:
    """数据库和 Redis 可连通才就绪，否则返回 503；结果缓存数秒。"""
    result = await request.app.state.readiness.get(request.app.state.container)
    return JSONResponse(result, status_code=200 if result["ready"] else 503)


@router.get("/health/details")
async def details(request: Request) -> dict:
    """详细诊断必须认证，避免公开依赖拓扑或配置来源。"""
    principal = await current_principal(request)
    principal.require("resources:read")
    return await diagnostics(request.app.state.container)


@router.get("/metrics")
async def metrics() -> Response:
    """只输出进程内指标，不访问数据库；运行数量等由后台任务周期写入。"""
    if os.getenv("PROMETHEUS_MULTIPROC_DIR"):
        registry = CollectorRegistry()
        multiprocess.MultiProcessCollector(registry)
        content = generate_latest(registry)
    else:
        content = generate_latest()
    return Response(content, headers={"Content-Type": CONTENT_TYPE_LATEST})
