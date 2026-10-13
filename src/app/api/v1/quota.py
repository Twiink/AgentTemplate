"""配额查询 API：查看当前月份的费用使用情况。"""

from typing import Annotated

from fastapi import APIRouter, Depends, Request

from app.core.security import Principal, current_principal

router = APIRouter(prefix="/quota", tags=["quota"])


@router.get("")
async def get_quota(
    request: Request,
    principal: Annotated[Principal, Depends(current_principal)],
):
    """查询当前主体的费用配额使用情况。"""
    container = request.app.state.container
    usage = await container.quota.usage(principal)
    return usage
