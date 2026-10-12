"""迁移版本链：核心迁移与各模块迁移是 Alembic 的独立分支，分支标签分别为 core 与模块名。

版本链以磁盘上的迁移脚本为准，供 migrate 与启动预检共同判断数据库版本。被 init --prune
删除的模块不再出现在版本链中。
"""

from __future__ import annotations

import os
from functools import lru_cache
from pathlib import Path

from alembic.config import Config
from alembic.script import ScriptDirectory

MIGRATIONS = Path(__file__).parent / "migrations"
CORE_BRANCH = "core"


def migration_locations() -> list[Path]:
    """核心迁移目录加上磁盘上全部模块的迁移目录；未启用的模块也包含在内，用于识别已有版本。"""
    from app.modules import CATALOG

    return [MIGRATIONS / "versions"] + [
        info.migrations for info in CATALOG.values() if info.migrations.is_dir()
    ]


def alembic_config() -> Config:
    """返回包含全部分支目录的 Alembic 配置。"""
    config = Config(str(MIGRATIONS / "alembic.ini"))
    config.set_main_option(
        "version_locations", os.pathsep.join(str(path) for path in migration_locations())
    )
    return config


@lru_cache(maxsize=1)
def script_directory() -> ScriptDirectory:
    """迁移脚本在进程运行期间不变，解析结果可以缓存。"""
    return ScriptDirectory.from_config(alembic_config())


def known_revisions() -> frozenset[str]:
    """全部分支上的已知版本。"""
    return frozenset(script.revision for script in script_directory().walk_revisions())


def branch_heads(branches: list[str]) -> frozenset[str]:
    """指定分支各自的最新版本；数据库升级完成后版本表应包含这些版本。"""
    directory = script_directory()
    return frozenset(directory.get_revision(f"{branch}@head").revision for branch in branches)
