"""路由公用依赖：带限流的可信身份、分页参数，以及 lifespan 中建立的资源容器。"""

from __future__ import annotations

from typing import Annotated

from fastapi import Depends, Query, Request

from app.core.container import Container
from app.core.ratelimit import limited_principal
from app.core.security import Principal

Identity = Annotated[Principal, Depends(limited_principal)]
Limit = Annotated[int, Query(ge=1, le=100)]
Offset = Annotated[int, Query(ge=0)]


def get_container(request: Request) -> Container:
    """返回 lifespan 中建立的资源容器。"""
    return request.app.state.container


Resources = Annotated[Container, Depends(get_container)]
