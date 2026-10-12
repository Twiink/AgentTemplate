"""可选模块：目录、运行时契约、依赖校验与加载。

目录只记录名称、依赖、extras 和表名等静态信息，导入本文件不会加载任何模块代码；
模块的服务、路由和任务在启用后由 load() 导入。两档预设的默认模块列表见 config/presets。
"""

from __future__ import annotations

import importlib
import importlib.util
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from fastapi import APIRouter

    from app.core.container import Container

MODULES_DIR = Path(__file__).parent


class ModuleError(ValueError):
    """模块配置无效：名称未知、缺少依赖模块，或模块代码已被 init --prune 删除。"""


@dataclass(frozen=True)
class ModuleInfo:
    """模块静态信息；配置校验、迁移、授权与启动预检都只依赖这些信息，不导入模块代码。"""

    name: str
    description: str
    requires: tuple[str, ...] = ()
    extra: str | None = None
    packages: tuple[str, ...] = ()
    tables: tuple[str, ...] = ()

    @property
    def path(self) -> Path:
        """模块代码所在目录。"""
        return MODULES_DIR / self.name

    @property
    def migrations(self) -> Path:
        """模块迁移目录，作为 Alembic 的独立分支，分支标签为模块名。"""
        return self.path / "migrations"

    @property
    def present(self) -> bool:
        """模块代码是否仍在磁盘上；执行 init --prune 删除后返回 False。"""
        return (self.path / "__init__.py").exists()


@dataclass
class Module:
    """模块运行时契约；每个模块在 app.modules.<名称> 中导出名为 module 的实例。

    setup 在核心资源建立后按依赖顺序执行，负责创建模块服务并向其他服务登记回调；
    router 返回挂在 /api/v1 下的路由；diagnose 为详细诊断补充模块状态。
    """

    info: ModuleInfo
    models: str | None = None
    router: Callable[[], APIRouter] | None = None
    setup: Callable[[Container], Awaitable[None]] | None = None
    diagnose: Callable[[Container], Awaitable[dict[str, Any]]] | None = None


# 顺序即依赖顺序：被依赖的模块排在前面，装配和迁移都按这个顺序执行。
CATALOG: dict[str, ModuleInfo] = {
    "storage": ModuleInfo(
        "storage",
        "附件存储：本地目录或 S3",
        tables=("artifacts",),
    ),
    "background": ModuleInfo(
        "background",
        "后台运行：outbox 投递、Celery Worker、租约恢复与定期清理",
        extra="background",
        packages=("celery",),
        tables=("outbox",),
    ),
    "knowledge": ModuleInfo(
        "knowledge",
        "RAG 知识库：文档导入、向量检索与索引重建",
        requires=("storage", "background"),
        extra="knowledge",
        packages=("pgvector", "pypdf", "docx", "bs4"),
        tables=("knowledge_bases", "knowledge_indexes", "documents", "import_jobs", "chunks"),
    ),
    "memory": ModuleInfo(
        "memory",
        "长期记忆：显式读写；启用 background 时支持后台提取",
        tables=("memories", "memory_extractions"),
    ),
}


def resolve(names: list[str]) -> list[ModuleInfo]:
    """校验启用的模块并按依赖顺序返回；名称未知、缺少依赖或代码已删除时抛出 ModuleError。"""
    unknown = [name for name in names if name not in CATALOG]
    if unknown:
        raise ModuleError("未知模块：" + "、".join(unknown))
    enabled = set(names)
    for name in names:
        missing = [item for item in CATALOG[name].requires if item not in enabled]
        if missing:
            raise ModuleError(f"模块 {name} 依赖未启用的模块：" + "、".join(missing))
    removed = [name for name in names if not CATALOG[name].present]
    if removed:
        raise ModuleError("模块代码已被删除：" + "、".join(removed))
    return [info for name, info in CATALOG.items() if name in enabled]


def missing_packages(info: ModuleInfo) -> list[str]:
    """返回模块所需但未安装的 Python 包；提示安装对应的 extras。"""
    return [package for package in info.packages if importlib.util.find_spec(package) is None]


def load(info: ModuleInfo) -> Module:
    """导入模块代码并返回其运行时契约。"""
    return importlib.import_module(f"app.modules.{info.name}").module


def module_tables(infos: list[ModuleInfo]) -> tuple[str, ...]:
    """汇总启用模块的表名，供迁移授权与行级安全检查使用。"""
    return tuple(table for info in infos for table in info.tables)
