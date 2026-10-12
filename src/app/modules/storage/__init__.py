"""storage 模块：附件内容保存在本地目录或 S3，数据库只保存元数据；其他模块可登记引用检查。"""

from __future__ import annotations

import asyncio
import tempfile
from typing import TYPE_CHECKING, Any

from app.modules import CATALOG, Module

if TYPE_CHECKING:
    from fastapi import APIRouter

    from app.core.container import Container


async def setup(container: Container) -> None:
    """按配置创建存储后端与附件服务；S3 客户端随资源容器一起关闭。"""
    from app.modules.storage.service import ArtifactService
    from app.modules.storage.store import LocalStore, S3Store

    config = container.settings.storage
    if config.backend == "local":
        store = LocalStore(config.directory)
    else:
        store = S3Store(config)
        container.stack.push_async_callback(store.close)
    container.storage = store
    container.artifacts = ArtifactService(container, store)


async def diagnose(container: Container) -> dict[str, Any]:
    """本地后端写入一个临时文件，S3 后端访问 bucket，确认存储可用。"""
    config = container.settings.storage
    try:
        if config.backend == "local":
            await asyncio.to_thread(config.directory.mkdir, parents=True, exist_ok=True)
            with tempfile.TemporaryFile(dir=config.directory) as handle:
                handle.write(b"storage-probe")
                handle.flush()
            return {"status": "verified", "backend": "local"}
        store: Any = container.storage
        await asyncio.to_thread(store.client.head_bucket, Bucket=store.bucket)
        return {"status": "verified", "backend": "s3"}
    except Exception as error:
        return {"status": "failed", "exception_type": type(error).__name__}


def router() -> APIRouter:
    """附件接口路由。"""
    from app.modules.storage.api import router

    return router


module = Module(
    CATALOG["storage"],
    models="app.modules.storage.models",
    router=router,
    setup=setup,
    diagnose=diagnose,
)
