"""命令共用的辅助函数：加载配置、启动前预检、写入私有文件。"""

from __future__ import annotations

import asyncio
import os
from pathlib import Path

import typer

from app.core.config import ConfigurationError, Settings, load_settings
from app.core.logging import setup_logging
from app.core.preflight import PreflightError, require_preflight


def settings_or_exit() -> Settings:
    """加载配置；配置错误时只输出字段与原因并以非零退出，不打印带输入值的异常堆栈。"""
    try:
        return load_settings()
    except ConfigurationError as error:
        typer.echo(f"[失败] 配置：{error}", err=True)
        raise typer.Exit(1) from None


def ensure_ready(settings: Settings) -> None:
    """长期运行的进程启动前执行预检；存在失败的阻断项时直接退出，详情已逐项写入日志。"""
    if not settings.preflight.enabled:
        return
    try:
        asyncio.run(require_preflight(settings, setup_logging(settings)))
    except PreflightError as error:
        typer.echo(str(error), err=True)
        raise typer.Exit(1) from None


def private_write(path: Path, content: str) -> bool:
    """用排他创建和 0600 权限写入私有文件；并发或重复初始化不会覆盖已有内容。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(descriptor, "w") as handle:
            handle.write(content)
    except FileExistsError:
        return False
    return True
