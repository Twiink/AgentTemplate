"""后台运行接口：在线程下提交运行并返回 202，由 Worker 异步执行。

只保留后台提交入口。人工响应不再单独提供接口：暂停的运行通过 `/threads/{id}/runs/wait`、
`/threads/{id}/runs/stream` 或再次提交 `resume` 继续，三种方式共用同一段恢复逻辑。
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends

from app.api.deps import Identity, Resources
from app.core.errors import AppError, Code
from app.modules.background.service import BackgroundService
from app.schemas.runs import RunInput, RunOut

router = APIRouter(tags=["runs"])


def get_background(container: Resources) -> BackgroundService:
    """取得后台运行服务；路由只在 background 模块启用时注册，服务缺失说明装配异常。"""
    if container.background is None:
        raise AppError(Code.MODULE_UNAVAILABLE, "background 模块未装配", 503)
    return container.background


Background = Annotated[BackgroundService, Depends(get_background)]


@router.post("/threads/{thread_id}/runs", status_code=202, response_model=RunOut)
async def create_run(
    thread_id: str, body: RunInput, principal: Identity, background: Background
) -> dict:
    """持久排队并返回运行；请求结束与连接断开都不影响 Worker 的执行。"""
    principal.require("resources:write")
    return await background.submit(principal, body.agent_id, thread_id, body.input, body.options)
