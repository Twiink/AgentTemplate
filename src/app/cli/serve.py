"""运行命令：开发服务器，以及 background 模块的 Worker 与调度进程。"""

from __future__ import annotations

import os

import typer

from app.cli.app import cli
from app.cli.common import ensure_ready, settings_or_exit
from app.core.config import Settings


def require_background(settings: Settings) -> None:
    """Worker 与调度进程只属于 background 模块；未启用时给出明确提示后退出。"""
    if "background" not in settings.modules:
        typer.echo(
            "background 模块未启用：在配置的 modules 中加入 background，或使用 full 预设", err=True
        )
        raise typer.Exit(1)


@cli.command()
def dev(host: str = "127.0.0.1", port: int = 8000):
    """启动 FastAPI 开发进程，保留重载能力。"""
    import uvicorn

    uvicorn.run("app.main:app", host=host, port=port, reload=True)


@cli.command()
def worker(concurrency: int = 2):
    """启动预检通过后启动 Celery Worker。"""
    settings = settings_or_exit()
    require_background(settings)
    ensure_ready(settings)
    if os.getenv("PROMETHEUS_MULTIPROC_DIR"):
        from prometheus_client import CollectorRegistry, multiprocess, start_http_server

        registry = CollectorRegistry()
        multiprocess.MultiProcessCollector(registry)
        start_http_server(9005, registry=registry)
    from app.modules.background.celery_app import celery_app

    celery_app.worker_main(["worker", "--loglevel=INFO", f"--concurrency={concurrency}"])


@cli.command()
def scheduler():
    """启动预检通过后启动任务投递、租约恢复与保留期清理调度；只部署一个 beat 实例。"""
    settings = settings_or_exit()
    require_background(settings)
    ensure_ready(settings)
    from app.modules.background.celery_app import celery_app

    celery_app.start(["beat", "--loglevel=INFO", "--schedule=.data/celerybeat"])
