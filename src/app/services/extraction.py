"""可选后台记忆提取；项目提供真实提取 Agent，模板处理来源、筛选、去重与重试。"""

from __future__ import annotations

import hashlib
import json
import re

from pydantic import BaseModel, Field
from sqlalchemy import select

from app.core.errors import AppError
from app.core.security import Principal
from app.db.models import Memory, MemoryExtraction, Outbox, Run
from app.services.data import attributes


class ExtractedMemory(BaseModel):
    """提取器输出记忆契约；合并键由项目定义，重复内容按摘要去重。"""

    namespace: str = Field(min_length=1, max_length=255)
    key: str = Field(min_length=1, max_length=255)
    content: str = Field(min_length=1, max_length=10000)
    metadata: dict = Field(default_factory=dict)


class ExtractionOutput(BaseModel):
    """真实提取注册项输出必须符合此 Schema。"""

    memories: list[ExtractedMemory]


class ExtractionService:
    """两阶段任务：执行提取运行，然后在同一事务幂等写入长期记忆。"""

    def __init__(self, resources):
        self.resources = resources
        self.db = resources.db

    async def schedule(self, session, run):
        """源运行成功后同事务建立任务；提取 Agent 自身不递归提取。"""
        registration = self.resources.settings.memory_extraction_registration
        if not registration or registration == run.agent_id:
            return
        job = MemoryExtraction(
            **attributes(Principal(run.tenant_id, run.subject_id, frozenset())),
            source_run_id=run.id,
        )
        session.add(job)
        await session.flush()
        session.add(
            Outbox(
                tenant_id=run.tenant_id,
                subject_id=run.subject_id,
                task="memory",
                resource_id=job.id,
            )
        )

    async def process(self, job_id):
        """使用来源运行的身份执行真实提取器；失败最多重试三次，永久错误可查询。"""
        async with self.db.transaction(system=True) as session:
            job = (
                await session.scalars(
                    select(MemoryExtraction).where(MemoryExtraction.id == job_id).with_for_update()
                )
            ).one_or_none()
            if job is None or job.status == "succeeded":
                return
            source = await session.get(Run, job.source_run_id)
            if source is None:
                raise AppError("memory_source_missing", "提取来源运行不存在")
            principal = Principal(source.tenant_id, source.subject_id, frozenset(source.scopes))
            extraction_id = job.extraction_run_id
            source_input, source_output, source_id = source.input, source.output, source.id
        try:
            if extraction_id is None:
                thread = await self.resources.data.create_thread(principal)
                run = await self.resources.runs.submit(
                    principal,
                    self.resources.settings.memory_extraction_registration,
                    thread["id"],
                    {"source_run_id": source_id, "input": source_input, "output": source_output},
                )
                async with self.db.transaction(principal) as session:
                    job = await session.get(MemoryExtraction, job_id)
                    job.extraction_run_id, job.status = run["id"], "running"
                extraction_id = run["id"]
            await self.resources.runs.execute(extraction_id)
            run = await self.resources.data.get(Run, extraction_id, principal)
            if run["status"] in {"queued", "running"}:
                async with self.db.transaction(principal) as session:
                    session.add(Outbox(**attributes(principal), task="memory", resource_id=job_id))
                return
            if run["status"] != "succeeded":
                raise AppError("memory_extractor_failed", "提取器未成功完成")
            result = ExtractionOutput.model_validate(run["output"])
            async with self.db.transaction(principal) as session:
                job = (
                    await session.scalars(
                        select(MemoryExtraction)
                        .where(MemoryExtraction.id == job_id)
                        .with_for_update()
                    )
                ).one()
                if job.status == "succeeded":
                    return
                for memory in result.memories:
                    # 提取默认拒绝常见秘密形式，不把模型筛选视为唯一保护层。
                    if re.search(
                        r"(?i)(api[_-]?key|password|secret|authorization|bearer|private key)\s*[:=]|sk-[a-zA-Z0-9]{12,}",
                        memory.content,
                    ):
                        continue
                    digest = hashlib.sha256(memory.content.encode()).hexdigest()
                    existing = (
                        await session.scalars(
                            select(Memory)
                            .where(
                                Memory.tenant_id == principal.tenant_id,
                                Memory.subject_id == principal.subject_id,
                                Memory.namespace == memory.namespace,
                                Memory.key == memory.key,
                            )
                            .with_for_update()
                        )
                    ).one_or_none()
                    if existing:
                        if existing.metadata_.get("content_sha256") != digest:
                            existing.content, existing.version = (
                                memory.content,
                                existing.version + 1,
                            )
                        sources = [
                            *existing.sources,
                            {"run_id": source_id, "extraction_run_id": extraction_id},
                        ]
                        existing.sources = list(
                            {json.dumps(v, sort_keys=True): v for v in sources}.values()
                        )
                        existing.metadata_ = {**memory.metadata, "content_sha256": digest}
                    else:
                        session.add(
                            Memory(
                                **attributes(principal),
                                namespace=memory.namespace,
                                key=memory.key,
                                content=memory.content,
                                sources=[{"run_id": source_id, "extraction_run_id": extraction_id}],
                                metadata_={**memory.metadata, "content_sha256": digest},
                            )
                        )
                job.status = "succeeded"
        except Exception as error:
            async with self.db.transaction(principal) as session:
                job = await session.get(MemoryExtraction, job_id)
                job.attempts += 1
                job.status = "failed" if job.attempts >= 3 else "queued"
                job.error = {
                    "code": error.code
                    if isinstance(error, AppError)
                    else "memory_extraction_failed",
                    "message": error.message if isinstance(error, AppError) else "记忆提取失败",
                }
                if job.status == "queued":
                    job.extraction_run_id = None
                    session.add(Outbox(**attributes(principal), task="memory", resource_id=job.id))
