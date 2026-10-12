"""命令行入口 agent-template；导入各命令模块即完成命令注册。"""

from app.cli import checks, database, evaluate, observability, project, serve  # noqa: F401
from app.cli.app import cli

__all__ = ["cli"]
