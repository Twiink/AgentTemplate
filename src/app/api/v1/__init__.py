"""/api/v1 核心路由汇总；各模块的路由在应用创建时按启用情况追加。"""

from fastapi import APIRouter

from app.api.v1 import agents, quota, runs, threads

router = APIRouter(prefix="/api/v1")
router.include_router(agents.router)
router.include_router(threads.router)
router.include_router(runs.router)
router.include_router(quota.router)
