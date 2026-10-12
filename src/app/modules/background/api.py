"""后台运行接口：提交运行（返回 202），以及提交人工响应后重新排队。"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends

from app.api.deps import Identity, Resources
from app.core.errors import AppError
from app.modules.background.service import BackgroundService
from app.schemas.runs import HumanInput, RunInput, RunOut

router = APIRouter(tags=["runs"])


def get_background(container: Resources) -> BackgroundService:
    """取得后台运行服务；路由只在 background 模块启用时注册，服务缺失说明装配异常。"""
    if container.background is None:
        raise AppError("module_unavailable", "background 模块未装配", 503)
    return container.background


Background = Annotated[BackgroundService, Depends(get_background)]


@router.post("/runs", status_code=202, response_model=RunOut)
async def create_run(body: RunInput, principal: Identity, background: Background) -> dict:
    """持久排队并返回运行；请求结束和连接断开不会影响 Worker。"""
    principal.require("resources:write")
    return await background.submit(principal, **body.model_dump())


@router.post("/runs/{run_id}/responses", status_code=202, response_model=RunOut)
async def human_response(
    run_id: str, body: HumanInput, principal: Identity, background: Background
) -> dict:
    """消费当前等待输入，恢复任务通过 outbox 投递。"""
    principal.require("resources:write")
    return await background.respond(principal, run_id, body.response, body.expected_sequence)
