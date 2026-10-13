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
    """指定分支各自的最新版本；供 migrate 决定需要升级到哪些版本。"""
    directory = script_directory()
    return frozenset(directory.get_revision(f"{branch}@head").revision for branch in branches)


def declared_parents(script) -> tuple[str, ...]:
    """迁移脚本声明的父版本：down_revision 与 depends_on。

    两者都会被 Alembic 当成归一化父版本处理：带 depends_on 的迁移不再新增版本行，
    而是用 UPDATE 把被依赖版本的版本行改写成自己。
    """
    values: list[str] = []
    for field in (script.down_revision, getattr(script, "dependencies", None)):
        if field is None:
            continue
        values.extend([field] if isinstance(field, str) else list(field))
    return tuple(values)


def pending_heads(branches: list[str], revisions: list[str]) -> frozenset[str]:
    """启用分支中尚未升级到最新版本的版本号；空集表示版本表符合预期。

    判断某个分支头是否已应用，不能只看它是否在版本表里。Alembic 只为「未被其他版本
    当作父版本」的版本保留独立行：带 depends_on 的迁移（例如 memory_0001 依赖
    core_0001）会用 UPDATE 把被依赖版本的版本行改写成自己。因此 core_0001 升到
    full 之后并不在版本表里，却已经应用。只比较字面值会误报缺少迁移。

    这里改为：分支头在版本表中，或者它是版本表中某个版本（含 depends_on）的祖先，
    都算已应用。这样也兼容模块增减：full 迁到 minimal 后，core 与 storage 的分支头
    被仍留在版本表中的 memory_0001、knowledge_0002 吸收，不会被判成未执行。
    """
    directory = script_directory()
    present = set(revisions)
    pending: set[str] = set()
    for branch in branches:
        head = directory.get_revision(f"{branch}@head").revision
        if head in present:
            continue
        # 从版本表中的每个版本向上走，看能否覆盖这个分支头。
        seen: set[str] = set()
        stack = list(present)
        applied = False
        while stack and not applied:
            revision = stack.pop()
            if revision in seen:
                continue
            seen.add(revision)
            for parent in declared_parents(directory.get_revision(revision)):
                if parent == head:
                    applied = True
                    break
                stack.append(parent)
        if not applied:
            pending.add(head)
    return frozenset(pending)
