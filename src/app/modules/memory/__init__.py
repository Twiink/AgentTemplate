"""memory 模块：长期记忆的显式读写；启用 background 时，运行成功后可在后台提取记忆。"""

from __future__ import annotations

from typing import TYPE_CHECKING

from app.modules import CATALOG, Module

if TYPE_CHECKING:
    from fastapi import APIRouter

    from app.core.container import Container


async def setup(container: Container) -> None:
    """创建记忆服务；background 已启用时接入记忆提取与过期清理。"""
    from app.modules.memory.service import MemoryService

    container.memories = MemoryService(container)
    await container.memories.initialize()
    if container.background is None:
        # 没有后台任务时无法执行提取，也没有定时清理；检索时会过滤已过期的记忆。
        return
    from app.modules.memory.extraction import ExtractionService

    container.extraction = ExtractionService(container)
    container.runs.succeeded_hooks.append(container.extraction.schedule)
    container.background.cleanup_hooks.append(container.memories.cleanup)


def router() -> APIRouter:
    """长期记忆接口路由。"""
    from app.modules.memory.api import router

    return router


module = Module(
    CATALOG["memory"],
    models="app.modules.memory.models",
    router=router,
    setup=setup,
)
