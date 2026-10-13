"""Celery 任务入口：每个 Worker 子进程建立一次常驻事件循环与资源容器，任务复用它执行。

旧实现为每个任务新建事件循环和资源容器，等于每次任务都要重连 PostgreSQL、Redis 与
MCP，连接池形同虚设。这里改为在 `worker_process_init` 时建立运行期，进程内只初始化
一次，任务把协程提交到该循环上执行；子进程退出时逆序关闭资源。

只在进程内建立一次，也意味着跨循环复用连接的问题不复存在：同一进程内的所有任务
共享同一个循环，模型 HTTP 客户端、数据库连接池和 MCP 会话都在该循环内创建。
"""

from __future__ import annotations

import asyncio
import logging
import threading
import time
from collections.abc import Callable, Coroutine
from concurrent.futures import Future
from typing import Any

from celery import Celery
from celery.signals import setup_logging as celery_setup_logging
from celery.signals import worker_process_init, worker_process_shutdown

from app.core.config import load_settings
from app.core.container import Container
from app.core.logging import setup_logging
from app.core.telemetry import worker_heartbeat

logger = logging.getLogger("app.worker")

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
        # 立即投递失败时由调度补发；提交路径已经在事务提交后主动投递，这里只做兜底。
        "dispatch": {"task": "agent.dispatch", "schedule": 30.0},
        "recover": {"task": "agent.recover", "schedule": 20.0},
        "cleanup": {"task": "agent.cleanup", "schedule": 3600.0},
    },
)


class WorkerRuntime:
    """单个 Worker 子进程的常驻运行期：一个事件循环，一个资源容器。

    容器在循环内进入、在同一循环内退出，因此数据库连接池、Redis 客户端与 MCP 会话
    始终绑定同一个循环。循环不能跑在任务所在线程上：`run_until_complete` 会在初始化
    结束时停止循环，之后再提交的协程永远不会被调度。因此这里把循环放进一个常驻线程
    里 `run_forever`，任务线程用 `run_coroutine_threadsafe` 提交并等待结果。
    """

    def __init__(self) -> None:
        self.loop: asyncio.AbstractEventLoop | None = None
        self.thread: threading.Thread | None = None
        self.container: Container | None = None
        self.ready = False
        self.background: list[asyncio.Task] = []

    async def _open(self) -> None:
        """建立资源容器并启动进程内周期任务；这里的失败必须让 Worker 启动失败。"""
        from app.services.health import run_metrics_collector

        container = Container(load_settings())
        self.container = await container.__aenter__()
        # 指标采集只属于本进程；/metrics 聚合由 Prometheus 多进程模式完成。
        self.background.append(asyncio.create_task(run_metrics_collector(self.container)))

    def start(self) -> None:
        """建立常驻循环线程并等待资源就绪；由 worker_process_init 调用一次。

        资源就绪之前 Worker 不会开始消费任务，因此初始化失败会直接让子进程启动失败，
        而不是把每个任务都变成一次失败重试。
        """
        self.loop = asyncio.new_event_loop()
        self.thread = threading.Thread(target=self._serve, name="worker-runtime", daemon=True)
        self.thread.start()
        # 初始化也提交到循环上执行，使数据库连接池与 Redis 客户端都建立在该循环内。
        asyncio.run_coroutine_threadsafe(self._open(), self.loop).result()
        self.ready = True
        logger.info("Worker 运行期已就绪", extra={"pid": self.pid()})

    def _serve(self) -> None:
        """循环线程主体：绑定该线程的事件循环并一直运行到 stop 请求。"""
        assert self.loop is not None
        asyncio.set_event_loop(self.loop)
        self.loop.run_forever()

    def submit(self, factory: Callable[[Container], Coroutine[Any, Any, Any]]) -> Any:
        """把协程提交到常驻循环并等待结果；超时与异常都原样抛给任务。"""
        if not self.ready or self.container is None or self.loop is None:
            raise RuntimeError("Worker 运行期尚未就绪，拒绝执行任务")
        future: Future = asyncio.run_coroutine_threadsafe(factory(self.container), self.loop)
        return future.result()

    async def _close(self) -> None:
        """先停止进程内周期任务，再逆序释放容器资源。"""
        for task in self.background:
            task.cancel()
        if self.background:
            await asyncio.gather(*self.background, return_exceptions=True)
        self.background.clear()
        if self.container is not None:
            await self.container.__aexit__(None, None, None)
            self.container = None

    def stop(self) -> None:
        """关闭资源与循环线程；重复调用是安全的。"""
        if not self.ready or self.loop is None:
            return
        try:
            asyncio.run_coroutine_threadsafe(self._close(), self.loop).result(timeout=30)
        except Exception as error:
            logger.warning("关闭 Worker 运行期失败", extra={"exception": type(error).__name__})
        finally:
            self.ready = False
            self.loop.call_soon_threadsafe(self.loop.stop)
            if self.thread is not None:
                self.thread.join(timeout=10)
            self.loop.close()
            self.loop, self.thread = None, None

    @staticmethod
    def pid() -> int | None:
        """当前进程号；用于确认每个子进程只初始化一次运行期。"""
        import os

        return os.getpid()


runtime = WorkerRuntime()


def required(service: Any, name: str) -> Any:
    """取得任务依赖的模块服务；模块未启用时直接失败，不把任务当作成功处理。"""
    if service is None:
        raise RuntimeError(f"{name} 模块未启用，任务无法执行")
    return service


def perform(action: Callable[[Container], Coroutine[Any, Any, Any]]) -> Any:
    """在常驻运行期上执行任务；心跳只在真实执行期间刷新。"""

    async def run(container: Container) -> Any:
        """包装实际动作，先记录心跳再交给业务协程。"""
        worker_heartbeat.set(time.time())
        return await action(container)

    return runtime.submit(run)


@celery_app.task(name="agent.run")
def execute_run(run_id: str):
    """执行已存在的运行，重复队列消息由 PostgreSQL claim 拒绝。"""
    return perform(lambda container: container.runs.execute(run_id))


@celery_app.task(name="agent.knowledge")
def ingest_document(job_id: str):
    """消费真实文档导入任务。"""
    return perform(lambda container: required(container.knowledge, "knowledge").ingest(job_id))


@celery_app.task(name="agent.dispatch")
def dispatch():
    """调度事务 outbox，不使用提交前发消息的竞态路径。"""
    return perform(lambda container: required(container.background, "background").dispatch())


@celery_app.task(name="agent.recover")
def recover():
    """定期收回失效运行租约，并执行各模块登记的租约回收。"""
    return perform(lambda container: required(container.background, "background").recover())


@celery_app.task(name="agent.cleanup")
def cleanup():
    """按配置清理终态运行、各模块登记的过期数据和已投递的 outbox 记录。"""
    return perform(lambda container: required(container.background, "background").cleanup())


@celery_app.task(name="agent.memory")
def extract_memory(job_id: str):
    """执行可选真实提取器并保存经筛选记忆，默认无提取任务。"""
    return perform(lambda container: required(container.extraction, "memory").process(job_id))


@worker_process_init.connect
def start_runtime(**kwargs):
    """子进程启动时建立一次运行期；失败会阻止该进程接收任务，而不是逐个任务反复失败。"""
    setup_logging(load_settings())
    runtime.start()


@worker_process_shutdown.connect
def stop_runtime(pid=None, **kwargs):
    """子进程退出时关闭运行期并清理活动 Gauge 文件。"""
    import os

    runtime.stop()
    if pid and os.getenv("PROMETHEUS_MULTIPROC_DIR"):
        from prometheus_client import multiprocess

        multiprocess.mark_process_dead(pid)


@celery_setup_logging.connect
def configure_logging(**kwargs):
    """接管 Celery 的日志初始化；连接此信号后 Celery 不再改写 root logger，输出与 API 相同格式。"""
    setup_logging(settings)
