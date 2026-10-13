"""附件接口：上传、列举、下载与删除。"""

from __future__ import annotations

from typing import Annotated
from urllib.parse import quote

from fastapi import APIRouter, Depends, UploadFile
from fastapi.responses import Response

from app.api.deps import Identity, Limit, Offset, Resources
from app.core.errors import AppError, Code
from app.modules.storage.models import Artifact
from app.modules.storage.schemas import ArtifactOut
from app.modules.storage.service import ArtifactService
from app.schemas.common import Page

router = APIRouter(tags=["artifacts"])


def get_artifacts(container: Resources) -> ArtifactService:
    """取得附件服务；路由只在 storage 模块启用时注册，服务缺失说明装配异常。"""
    if container.artifacts is None:
        raise AppError(Code.MODULE_UNAVAILABLE, "storage 模块未装配", 503)
    return container.artifacts


Artifacts = Annotated[ArtifactService, Depends(get_artifacts)]


@router.post("/artifacts", status_code=201, response_model=ArtifactOut)
async def upload_artifact(
    principal: Identity, container: Resources, artifacts: Artifacts, file: UploadFile
) -> dict:
    """有限读取上传内容，超限时中止，不把用户文件名用作对象路径。"""
    principal.require("resources:write")
    content = await file.read(container.settings.storage.max_bytes + 1)
    await file.close()
    return await artifacts.upload(
        principal, file.filename or "附件", file.content_type or "application/octet-stream", content
    )


@router.get("/artifacts", response_model=Page[ArtifactOut])
async def list_artifacts(
    principal: Identity, container: Resources, limit: Limit = 50, offset: Offset = 0
) -> dict:
    """列举当前主体附件元数据。"""
    principal.require("resources:read")
    return {"items": await container.repository.list(Artifact, principal, limit, offset)}


@router.get("/artifacts/{artifact_id}")
async def download_artifact(
    artifact_id: str, principal: Identity, artifacts: Artifacts
) -> Response:
    """校验归属后读取真实内容，文件名通过 RFC 编码写入下载头。"""
    principal.require("resources:read")
    metadata, content = await artifacts.download(principal, artifact_id)
    return Response(
        content,
        media_type=metadata["content_type"],
        headers={
            "Content-Disposition": "attachment; filename*=UTF-8''"
            + quote(metadata["filename"], safe="")
        },
    )


@router.delete("/artifacts/{artifact_id}", status_code=204)
async def delete_artifact(artifact_id: str, principal: Identity, artifacts: Artifacts) -> Response:
    """删除未被其他资源引用的真实附件。"""
    principal.require("resources:write")
    await artifacts.delete(principal, artifact_id)
    return Response(status_code=204)
