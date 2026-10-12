"""启动预检：开始服务之前确认配置、模块、目录、数据库、迁移与 Redis 可用；阻断项失败时停止启动。

预检使用独立的临时连接，不依赖资源容器，在资源初始化之前就能发现问题。只做只读检查，
不创建目录、不执行迁移、不调用收费模型。
"""

from __future__ import annotations

import asyncio
import os
import stat
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Literal

from app.core.config import Settings

Status = Literal["passed", "warning", "failed", "skipped"]
LABELS: dict[str, str] = {"passed": "通过", "warning": "警告", "failed": "失败", "skipped": "跳过"}


@dataclass(frozen=True)
class CheckResult:
    """单项检查结果；status 为 failed 且 blocking 为真时阻断启动。"""

    name: str
    status: Status
    message: str
    hint: str | None = None
    blocking: bool = True


@dataclass
class PreflightReport:
    """预检汇总；ok 表示没有失败的阻断项，警告不影响启动。"""

    results: list[CheckResult] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        """没有失败的阻断项时为真。"""
        return not any(item.status == "failed" and item.blocking for item in self.results)

    def as_dict(self) -> dict[str, Any]:
        """供 --json 输出与详细健康检查使用。"""
        return {"ok": self.ok, "checks": [asdict(item) for item in self.results]}

    def render(self) -> str:
        """生成逐行的中文检查结果，失败和警告附带修复建议。"""
        lines = []
        for item in self.results:
            lines.append(f"[{LABELS[item.status]}] {item.name}：{item.message}")
            if item.hint and item.status in {"warning", "failed"}:
                lines.append(f"    修复建议：{item.hint}")
        lines.append("预检通过。" if self.ok else "预检未通过：存在失败的阻断项。")
        return "\n".join(lines)


class PreflightError(RuntimeError):
    """启动预检未通过；消息只列出失败项名称，详情已逐项写入日志。"""


def default_value(name: str) -> str:
    """读取 Settings 中秘密字段的内置默认值，用于判断是否仍在使用未配置的连接。"""
    return Settings.model_fields[name].get_default().get_secret_value()


def nearest_existing(path: Path) -> Path:
    """返回路径自身或最近一个已存在的上级目录；目录尚未创建时检查其上级是否可写。"""
    current = path.resolve()
    while not current.exists() and current != current.parent:
        current = current.parent
    return current


def check_models(settings: Settings) -> list[CheckResult]:
    """逐个别名输出配置状态；不发起收费调用，只在缺少配置或价格时给出提示。"""
    from app.llm.pricing import load_pricing

    models, embeddings = settings.llm.models, settings.llm.embeddings
    if not models and not embeddings:
        return [
            CheckResult(
                "模型",
                "warning",
                "未配置任何模型别名，基础服务可以启动，Agent 运行前需要配置",
                "在 config/private.yaml 的 llm.models 与 llm.embeddings 中添加别名",
                blocking=False,
            )
        ]
    table = load_pricing(settings.llm.pricing_file)
    results = []
    for kind, alias_table in (("聊天", models), ("向量", embeddings)):
        for alias, profile in alias_table.items():
            provider = "openai" if profile.provider == "openai_compatible" else profile.provider
            notes = []
            if profile.api_key is None and profile.provider != "openai_compatible":
                # 只有兼容接口允许不带密钥，其他服务商缺密钥会在首次调用时失败。
                notes.append("未配置密钥")
            if table.get(provider, profile.model) is None and profile.price_version is not None:
                notes.append("价格表未收录")
            status: Status = "warning" if notes else "passed"
            message = f"{profile.provider}/{profile.model}"
            if notes:
                message += "（" + "、".join(notes) + "）"
            results.append(
                CheckResult(
                    f"{kind}模型 {alias}",
                    status,
                    message,
                    f"在 config/pricing.yaml 中补充 {provider}/{profile.model} 的单价"
                    if "价格表未收录" in notes
                    else None,
                    blocking=False,
                )
            )
    return results


def check_configuration(settings: Settings) -> list[CheckResult]:
    """配置已通过加载校验；这里补充私有配置文件权限与模型配置情况。"""
    files = [source for source in settings.config_sources if source.endswith(".yaml")]
    results = [CheckResult("配置", "passed", "已加载 " + ("、".join(files) or "内置默认值"))]
    for source in files:
        path = Path(source)
        if path.name == "private.yaml" and path.stat().st_mode & (stat.S_IRWXG | stat.S_IRWXO):
            results.append(
                CheckResult(
                    "私有配置权限",
                    "warning",
                    f"{source} 允许其他用户访问",
                    f"执行 chmod 600 {source}",
                    blocking=False,
                )
            )
    results.extend(check_models(settings))
    return results


def check_modules(settings: Settings) -> list[CheckResult]:
    """启用的模块必须存在、依赖齐全、代码未被删除，所需的 extras 已安装。"""
    import importlib.util

    from app.modules import ModuleError, missing_packages, resolve

    try:
        infos = resolve(settings.modules)
    except ModuleError as error:
        return [CheckResult("模块", "failed", str(error), "修改配置中的 modules 列表或预设")]
    results = []
    for info in infos:
        missing = missing_packages(info)
        if missing:
            results.append(
                CheckResult(
                    f"模块 {info.name}",
                    "failed",
                    "缺少依赖：" + "、".join(missing),
                    f"执行 uv sync --extra {info.extra}",
                )
            )
    if (
        "storage" in settings.modules
        and settings.storage.backend == "s3"
        and importlib.util.find_spec("boto3") is None
    ):
        results.append(
            CheckResult("模块 storage", "failed", "S3 后端缺少 boto3", "执行 uv sync --extra s3")
        )
    names = "、".join(info.name for info in infos) or "无"
    return results or [CheckResult("模块", "passed", f"预设 {settings.preset}，已启用：{names}")]


def check_environment(settings: Settings) -> list[CheckResult]:
    """连接配置不能停留在内置默认值；生产环境视为阻断，其他环境只提示。"""
    missing = [
        name
        for name in ("database_url", "redis_url")
        if getattr(settings, name).get_secret_value() == default_value(name)
    ]
    if not missing:
        return [CheckResult("连接配置", "passed", "数据库与 Redis 连接已配置")]
    production = settings.environment == "production"
    return [
        CheckResult(
            "连接配置",
            "failed" if production else "warning",
            "仍在使用内置默认值：" + "、".join(missing),
            "生产环境通过环境变量或 Secret 注入 AGENT_DATABASE_URL、AGENT_REDIS_URL"
            if production
            else "执行 agent-template init 生成私有配置",
            blocking=production,
        )
    ]


def check_directories(settings: Settings) -> list[CheckResult]:
    """运行期写入的目录必须可写；Prompt 目录缺失只提示，不阻断启动。"""
    targets = [("工具工作目录", settings.tools.directory)]
    if settings.storage.backend == "local":
        targets.append(("附件目录", settings.storage.directory))
    results = []
    for name, path in targets:
        existing = nearest_existing(path)
        if os.access(existing, os.W_OK):
            results.append(CheckResult(name, "passed", f"{path} 可写"))
        else:
            results.append(
                CheckResult(name, "failed", f"{path} 不可写", f"检查 {existing} 的属主和权限")
            )
    if not settings.prompts_directory.is_dir():
        results.append(
            CheckResult(
                "Prompt 目录",
                "warning",
                f"{settings.prompts_directory} 不存在，加载 Prompt 时会失败",
                "创建该目录，或修改配置项 prompts_directory",
                blocking=False,
            )
        )
    return results


def migration_result(
    settings: Settings, tables: set[str], revisions: list[str], branches: list[str]
) -> CheckResult:
    """比较数据库版本与代码中的迁移版本链；核心与每个启用模块的分支都要升级到最新版本。"""
    from app.db.revisions import branch_heads, known_revisions

    if "alembic_version" not in tables:
        if tables:
            return CheckResult(
                "迁移",
                "failed",
                "发现未登记迁移版本的历史表",
                "备份后人工迁移；migrate 不会修改这类数据库",
            )
        return CheckResult("迁移", "failed", "数据库尚未初始化", "执行 agent-template migrate")
    known = known_revisions()
    heads = branch_heads(branches)
    unknown = [revision for revision in revisions if revision not in known]
    if not revisions or unknown:
        return CheckResult(
            "迁移",
            "failed",
            "数据库版本不属于本代码的迁移版本链：" + ("、".join(unknown) or "版本表为空"),
            "确认部署的代码版本；数据库可能来自更新的版本或其他项目",
        )
    current = "、".join(sorted(revisions))
    if not heads <= set(revisions):
        failing = settings.preflight.pending_migrations == "fail"
        return CheckResult(
            "迁移",
            "failed" if failing else "warning",
            f"存在未执行的迁移：当前 {current}，目标 {'、'.join(sorted(heads))}",
            "执行 agent-template migrate",
            blocking=failing,
        )
    return CheckResult("迁移", "passed", f"当前版本 {current}")


async def check_database(settings: Settings) -> list[CheckResult]:
    """检查连通性、运行角色、迁移版本与行级安全；使用不入池的临时连接，结束即释放。"""
    from sqlalchemy import bindparam, pool, text
    from sqlalchemy.ext.asyncio import create_async_engine

    from app.db.models import CORE_TABLES
    from app.db.revisions import CORE_BRANCH
    from app.modules import ModuleError, module_tables, resolve

    try:
        infos = resolve(settings.modules)
    except ModuleError:
        # 模块配置错误已由模块检查报告，这里只检查核心表。
        infos = []
    branches = [CORE_BRANCH, *(info.name for info in infos)]
    timeout = settings.preflight.timeout_seconds
    engine = create_async_engine(
        settings.database_url.get_secret_value(),
        poolclass=pool.NullPool,
        connect_args={"timeout": timeout},
    )
    owned = sorted([*CORE_TABLES, *module_tables(infos)])
    try:
        async with asyncio.timeout(timeout), engine.connect() as connection:
            version = await connection.scalar(text("SHOW server_version"))
            role = (
                await connection.execute(
                    text("SELECT rolsuper, rolbypassrls FROM pg_roles WHERE rolname = current_user")
                )
            ).one()
            tables = set(
                await connection.scalars(
                    text("SELECT tablename FROM pg_tables WHERE schemaname = 'public'")
                )
            )
            revisions = (
                list(await connection.scalars(text("SELECT version_num FROM alembic_version")))
                if "alembic_version" in tables
                else []
            )
            unprotected = list(
                await connection.scalars(
                    text(
                        "SELECT relname FROM pg_class WHERE relnamespace = 'public'::regnamespace "
                        "AND relname IN :names AND NOT (relrowsecurity AND relforcerowsecurity)"
                    ).bindparams(bindparam("names", expanding=True)),
                    {"names": owned},
                )
            )
    except Exception as error:
        return [
            CheckResult(
                "PostgreSQL",
                "failed",
                f"无法连接（{type(error).__name__}）",
                "确认数据库已启动，并检查 database_url 中的地址、端口、用户名和密码",
            ),
            CheckResult("迁移", "skipped", "依赖数据库连接", blocking=False),
        ]
    finally:
        await engine.dispose()
    results = [CheckResult("PostgreSQL", "passed", f"已连接，服务端版本 {version}")]
    if role.rolsuper or role.rolbypassrls:
        results.append(
            CheckResult(
                "数据库角色",
                "failed",
                "运行连接使用了超级用户或具有 BYPASSRLS 的角色，行级隔离不会生效",
                "database_url 使用 agent_app 这类普通角色；迁移连接另配 migration_database_url",
            )
        )
    else:
        results.append(CheckResult("数据库角色", "passed", "运行角色受行级安全约束"))
    results.append(migration_result(settings, tables, revisions, branches))
    if unprotected:
        results.append(
            CheckResult(
                "行级安全",
                "failed",
                "以下表未启用强制行级安全：" + "、".join(unprotected),
                "执行 agent-template migrate；不要手工关闭表的 RLS",
            )
        )
    elif revisions:
        results.append(
            CheckResult("行级安全", "passed", "核心表与已启用模块的表均已启用强制行级安全")
        )
    return results


async def check_redis(settings: Settings) -> list[CheckResult]:
    """确认 Redis 可连通；当前 Celery 队列与限流都依赖它，因此作为阻断项。"""
    from redis.asyncio import Redis

    timeout = settings.preflight.timeout_seconds
    client = Redis.from_url(
        settings.redis_url.get_secret_value(),
        socket_connect_timeout=timeout,
        socket_timeout=timeout,
    )
    try:
        async with asyncio.timeout(timeout):
            await client.ping()
    except Exception as error:
        return [
            CheckResult(
                "Redis",
                "failed",
                f"无法连接（{type(error).__name__}）",
                "确认 Redis 已启动，并检查 redis_url",
            )
        ]
    finally:
        await client.aclose()
    return [CheckResult("Redis", "passed", "已连接")]


async def run_preflight(settings: Settings) -> PreflightReport:
    """依次执行全部检查；数据库与 Redis 的网络检查并行进行。"""
    report = PreflightReport()
    report.results += check_configuration(settings)
    report.results += check_modules(settings)
    report.results += check_environment(settings)
    report.results += check_directories(settings)
    database, redis = await asyncio.gather(check_database(settings), check_redis(settings))
    report.results += database + redis
    return report


async def require_preflight(settings: Settings, logger: Any) -> PreflightReport:
    """启动入口使用：逐项写日志，存在失败的阻断项时抛出 PreflightError 阻止启动。"""
    report = await run_preflight(settings)
    for item in report.results:
        if item.status == "failed":
            logger.error("启动预检失败", check=item.name, detail=item.message, hint=item.hint)
        elif item.status == "warning":
            logger.warning("启动预检警告", check=item.name, detail=item.message, hint=item.hint)
    if not report.ok:
        failed = [item.name for item in report.results if item.status == "failed" and item.blocking]
        raise PreflightError("启动预检未通过：" + "、".join(failed))
    logger.info("启动预检通过")
    return report
