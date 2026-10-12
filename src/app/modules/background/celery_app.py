"""Celery 真实任务入口；每次任务使用独立事件循环与资源生命周期，避免跨循环连接复用。"""

import asyncio
import time
from collections.abc import Awaitable, Callable
from typing import Any

from celery import Celery
from celery.signals import setup_logging as celery_setup_logging
from celery.signals import worker_process_shutdown

from app.core.config import load_settings
from app.core.container import Container
from app.core.logging import setup_logging
from app.core.telemetry import worker_heartbeat

settings = load_settings()
celery_app = Celery("agent_template", broker=settings.redis_url.get_secret_value())
celery_app.conf.update(
    task_serializer="json",
    accept_content=["json"],
    result_serializer="json",
    timezone="UTC",
    enable_utc=True,
    task_acks_late=True,
    task_reject_on_worker_lost=True,
    worker_prefetch_multiplier=1,
    task_ignore_result=True,
    task_time_limit=settings.run_timeout_seconds + 120,
    broker_transport_options={"visibility_timeout": settings.run_timeout_seconds + 120},
    beat_schedule={
        "dispatch": {"task": "agent.dispatch", "schedule": 2.0},
        "recover": {"task": "agent.recover", "schedule": 20.0},
        "cleanup": {"task": "agent.cleanup", "schedule": 3600.0},
    },
)


def required(service: Any, name: str) -> Any:
    """取得任务依赖的模块服务；模块未启用时直接失败，不把任务当作成功处理。"""
    if service is None:
        raise RuntimeError(f"{name} 模块未启用，任务无法执行")
    return service


async def perform(action: Callable[[Container], Awaitable[Any]]) -> Any:
    """为单个任务建立资源容器并执行；退出任务时关闭数据库、Redis、HTTP 与 MCP。"""
    async with Container(load_settings()) as container:
        worker_heartbeat.set(time.time())
        return await action(container)


@celery_app.task(name="agent.run")
def execute_run(run_id: str):
    """执行已存在的运行，重复队列消息由 PostgreSQL claim 拒绝。"""
    return asyncio.run(perform(lambda container: container.runs.execute(run_id)))


@celery_app.task(name="agent.knowledge")
def ingest_document(job_id: str):
    """消费真实文档导入任务。"""
    return asyncio.run(
        perform(lambda container: required(container.knowledge, "knowledge").ingest(job_id))
    )


@celery_app.task(name="agent.dispatch")
def dispatch():
    """调度事务 outbox，不使用提交前发消息的竞态路径。"""
    return asyncio.run(
        perform(lambda container: required(container.background, "background").dispatch())
    )


@celery_app.task(name="agent.recover")
def recover():
    """定期收回失效运行租约，并执行各模块登记的租约回收。"""
    return asyncio.run(
        perform(lambda container: required(container.background, "background").recover())
    )


@celery_app.task(name="agent.cleanup")
def cleanup():
    """按配置清理终态运行、各模块登记的过期数据和已投递的 outbox 记录。"""
    return asyncio.run(
        perform(lambda container: required(container.background, "background").cleanup())
    )


@celery_app.task(name="agent.memory")
def extract_memory(job_id: str):
    """执行可选真实提取器并保存经筛选记忆，默认无提取任务。"""
    return asyncio.run(
        perform(lambda container: required(container.extraction, "memory").process(job_id))
    )


@celery_setup_logging.connect
def configure_logging(**kwargs):
    """接管 Celery 的日志初始化；连接此信号后 Celery 不再改写 root logger，输出与 API 相同格式。"""
    setup_logging(settings)


@worker_process_shutdown.connect
def clear_worker_metrics(pid=None, **kwargs):
    """子进程退出时清理活动 Gauge 文件，累计 Counter 保留到容器生命周期结束。"""
    import os

    if pid and os.getenv("PROMETHEUS_MULTIPROC_DIR"):
        from prometheus_client import multiprocess

        multiprocess.mark_process_dead(pid)
