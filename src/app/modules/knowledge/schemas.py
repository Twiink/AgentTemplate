"""知识库接口的请求与响应模型；导入租约等内部字段不会出现在响应中。"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field


class KnowledgeInput(BaseModel):
    """知识库创建需明确选择独立 Embedding 配置。"""

    name: str = Field(min_length=1, max_length=255)
    embedding: str


class ImportInput(BaseModel):
    """附件输入与切分参数；同名文档关联新版本。"""

    artifact_id: str
    name: str = Field(min_length=1, max_length=255)
    chunk_size: int = Field(default=1200, ge=100, le=10000)
    overlap: int = Field(default=150, ge=0)


class SearchInput(BaseModel):
    """检索参数不包含身份，过滤条件只能缩小当前主体的结果范围。"""

    query: str = Field(min_length=1, max_length=20000)
    top_k: int = Field(default=5, ge=1, le=50)
    metadata: dict[str, Any] = Field(default_factory=dict)
    rerank: bool = False


class RebuildInput(BaseModel):
    """索引重建目标配置名称。"""

    embedding: str


class KnowledgeBaseOut(BaseModel):
    """知识库及当前提供查询的向量空间。"""

    id: str
    created_at: datetime
    name: str
    active_index: str | None = None


class KnowledgeIndexOut(BaseModel):
    """不可变向量空间；重建完成前状态为 building。"""

    id: str
    created_at: datetime
    knowledge_id: str
    profile: dict[str, Any]
    status: str


class DocumentOut(BaseModel):
    """知识库中的文档；同名导入会生成新版本。"""

    id: str
    created_at: datetime
    knowledge_id: str
    name: str
    version: int


class ImportJobOut(BaseModel):
    """导入任务的状态、进度与失败分类。"""

    id: str
    created_at: datetime
    knowledge_id: str
    index_id: str
    artifact_id: str | None = None
    name: str
    status: str
    progress: int
    cancel_requested: bool
    error: dict[str, Any] | None = None
    config: dict[str, Any] = Field(default_factory=dict)


class SearchHit(BaseModel):
    """检索结果片段及其出处。"""

    content: str
    distance: float
    document_id: str
    version: int
    position: int
    page: int | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)
