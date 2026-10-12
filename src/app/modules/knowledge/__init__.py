"""knowledge 模块：文档导入、向量检索与索引重建；依赖 storage 读取附件、依赖 background 执行导入。"""

from __future__ import annotations

from typing import TYPE_CHECKING

from app.modules import CATALOG, Module, ModuleError

if TYPE_CHECKING:
    from fastapi import APIRouter

    from app.core.container import Container


async def setup(container: Container) -> None:
    """创建知识服务，向 background 登记导入租约回收，向附件服务登记引用检查。"""
    from app.modules.knowledge.service import KnowledgeService

    if container.background is None or container.artifacts is None:
        raise ModuleError("knowledge 模块需要先装配 storage 与 background")
    service = KnowledgeService(container)
    container.knowledge = service
    container.background.recovery_hooks.append(service.recover)
    container.artifacts.guards.append(service.artifact_in_use)


def router() -> APIRouter:
    """知识库接口路由。"""
    from app.modules.knowledge.api import router

    return router


module = Module(
    CATALOG["knowledge"],
    models="app.modules.knowledge.models",
    router=router,
    setup=setup,
)
