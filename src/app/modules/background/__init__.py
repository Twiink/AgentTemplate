"""background 模块：后台运行的提交与人工响应、outbox 投递、Celery Worker、租约恢复与定期清理。"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Any

from app.modules import CATALOG, Module

if TYPE_CHECKING:
    from fastapi import APIRouter

    from app.core.container import Container


async def setup(container: Container) -> None:
    """创建后台运行服务；其他模块在各自的 setup 中向它登记恢复与清理任务。"""
    from app.modules.background.service import BackgroundService

    container.background = BackgroundService(container)


async def diagnose(container: Container) -> dict[str, Any]:
    """向 Worker 发送 ping，确认至少一个 Worker 在线。"""
    try:
        from app.modules.background.celery_app import celery_app

        workers = await asyncio.to_thread(celery_app.control.ping, timeout=2)
        return {"status": "verified" if workers else "failed", "workers": len(workers)}
    except Exception as error:
        return {"status": "failed", "exception_type": type(error).__name__}


def router() -> APIRouter:
    """后台运行接口路由。"""
    from app.modules.background.api import router

    return router


module = Module(
    CATALOG["background"],
    models="app.modules.background.models",
    router=router,
    setup=setup,
    diagnose=diagnose,
)
