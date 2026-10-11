"""Celery 真实任务入口；每次任务使用独立事件循环与资源生命周期，避免跨循环连接复用。"""

import asyncio
import time

from celery import Celery
from celery.signals import worker_process_shutdown

from app.core.config import load_settings
from app.core.resources import Resources

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


async def perform(method: str, *args):
    """接通真实服务方法，退出任务时关闭数据库、Redis、HTTP 与 MCP。"""
    resources = Resources(load_settings())
    try:
        async with resources:
            from app.core.observability import worker_heartbeat

            worker_heartbeat.set(time.time())
            service = (
                resources.knowledge
                if method == "ingest"
                else resources.extraction
                if method == "process"
                else resources.runs
            )
            return await getattr(service, method)(*args)
    finally:
        await resources.stack.aclose()


@celery_app.task(name="agent.run")
def execute_run(run_id: str):
    """执行已存在的运行，重复队列消息由 PostgreSQL claim 拒绝。"""
    return asyncio.run(perform("execute", run_id))


@celery_app.task(name="agent.knowledge")
def ingest_document(job_id: str):
    """消费真实文档导入任务。"""
    return asyncio.run(perform("ingest", job_id))


@celery_app.task(name="agent.dispatch")
def dispatch():
    """调度事务 outbox，不使用提交前发消息的竞态路径。"""
    return asyncio.run(perform("dispatch"))


@celery_app.task(name="agent.recover")
def recover():
    """定期收回失效运行租约。"""
    return asyncio.run(perform("recover"))


@celery_app.task(name="agent.cleanup")
def cleanup():
    """按配置清理终态运行、事件和过期记忆。"""
    return asyncio.run(perform("cleanup"))


@celery_app.task(name="agent.memory")
def extract_memory(job_id: str):
    """执行可选真实提取器并保存经筛选记忆，默认无提取任务。"""
    return asyncio.run(perform("process", job_id))


@worker_process_shutdown.connect
def clear_worker_metrics(pid=None, **kwargs):
    """子进程退出时清理活动 Gauge 文件，累计 Counter 保留到容器生命周期结束。"""
    import os

    if pid and os.getenv("PROMETHEUS_MULTIPROC_DIR"):
        from prometheus_client import multiprocess

        multiprocess.mark_process_dead(pid)
