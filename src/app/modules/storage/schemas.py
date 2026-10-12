"""附件接口的响应模型；对象键等内部字段不会出现在响应中。"""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel


class ArtifactOut(BaseModel):
    """附件元数据；filename 仅用于展示和下载头，不参与存储路径。"""

    id: str
    created_at: datetime
    filename: str
    content_type: str
    sha256: str
    size: int
