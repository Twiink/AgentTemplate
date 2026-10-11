"""健康与配置诊断；探针只执行低成本基础设施查询，不调用收费模型。"""

import asyncio
import time

from sqlalchemy import text

from app.db.models import Base


async def diagnostics(resources, detailed=False):
    """区分成功、失败、未配置与未验证；异常文本不输出秘密连接参数。"""
    results = {}

    async def database():
        """检查迁移、连接角色与版本，不执行收费或写入操作。"""
        started = time.monotonic()
        try:
            async with resources.db.engine.connect() as connection:
                version = (await connection.execute(text("SHOW server_version"))).scalar_one()
                tables = set(
                    (
                        await connection.execute(
                            text("SELECT tablename FROM pg_tables WHERE schemaname='public'")
                        )
                    ).scalars()
                )
                if not set(Base.metadata.tables) <= tables:
                    return {"status": "failed", "message": "数据库尚未完成模板迁移"}
                from pathlib import Path

                from alembic.config import Config
                from alembic.script import ScriptDirectory

                config = Config(str(Path(__file__).parents[1] / "db/migrations/alembic.ini"))
                expected_revision = ScriptDirectory.from_config(config).get_current_head()
                revision = (
                    await connection.execute(text("SELECT version_num FROM alembic_version"))
                ).scalar_one()
                if revision != expected_revision:
                    return {"status": "failed", "message": "数据库迁移版本不是当前模板版本"}
                role = (
                    await connection.execute(
                        text(
                            "SELECT rolsuper, rolbypassrls FROM pg_roles WHERE rolname=current_user"
                        )
                    )
                ).one()
                if role.rolsuper or role.rolbypassrls:
                    return {
                        "status": "failed",
                        "message": "运行连接必须使用非超级用户且无 BYPASSRLS 的角色",
                    }
            return {
                "status": "verified",
                "version": version,
                "revision": revision,
                "latency_ms": round((time.monotonic() - started) * 1000, 2),
            }
        except Exception as error:
            return {
                "status": "failed",
                "message": "数据库检查失败",
                "exception_type": type(error).__name__,
            }

    async def redis():
        """用 PING 确认缓存与队列服务可达。"""
        try:
            await resources.redis.ping()
            return {"status": "verified"}
        except Exception as error:
            return {
                "status": "failed",
                "message": "Redis 检查失败",
                "exception_type": type(error).__name__,
            }

    results["database"], results["redis"] = await asyncio.gather(database(), redis())
    if detailed:
        results["configuration"] = {
            "status": "verified",
            "sources": resources.settings.config_sources,
        }
        results["authentication"] = {
            "status": "configured"
            if resources.settings.auth.api_keys
            or resources.settings.auth.jwt_key
            or resources.settings.auth.jwks_url
            else "unconfigured"
        }
        results["models"] = {
            "status": "configured_unverified" if resources.settings.chat_models else "unconfigured",
            "profiles": list(resources.settings.chat_models),
        }
        results["embeddings"] = {
            "status": "configured_unverified"
            if resources.settings.embedding_models
            else "unconfigured",
            "profiles": list(resources.settings.embedding_models),
        }
        try:
            if resources.settings.storage.backend == "local":
                directory = resources.settings.storage.directory
                await asyncio.to_thread(directory.mkdir, parents=True, exist_ok=True)
                import tempfile

                with tempfile.TemporaryFile(dir=directory) as handle:
                    handle.write(b"storage-probe")
                    handle.flush()
                results["storage"] = {"status": "verified", "backend": "local"}
            else:
                await asyncio.to_thread(
                    resources.storage.client.head_bucket, Bucket=resources.storage.bucket
                )
                results["storage"] = {"status": "verified", "backend": "s3"}
        except Exception as error:
            results["storage"] = {"status": "failed", "exception_type": type(error).__name__}
        try:
            from app.worker.celery import celery_app

            workers = await asyncio.to_thread(celery_app.control.ping, timeout=2)
            results["worker"] = {
                "status": "verified" if workers else "failed",
                "count": len(workers),
            }
        except Exception as error:
            results["worker"] = {"status": "failed", "exception_type": type(error).__name__}
        results["tracing"] = {
            "status": "configured_unverified"
            if resources.settings.tracing.enabled
            else "unconfigured"
        }
        results["agents"] = {
            "status": "configured" if resources.agents.describe() else "unconfigured",
            "count": len(resources.agents.describe()),
        }
        results["mcp"] = {}
        for name in resources.settings.mcp:
            try:
                await resources.mcp.connect(name)
                results["mcp"][name] = {"status": "verified"}
            except Exception as error:
                results["mcp"][name] = {"status": "failed", "exception_type": type(error).__name__}
    ready = all(results[key]["status"] == "verified" for key in ["database", "redis"])
    return {"ready": ready, "components": results}
