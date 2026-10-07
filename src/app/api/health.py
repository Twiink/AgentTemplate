"""Health-check endpoints."""

from fastapi import APIRouter

from app.dependencies import SettingsDep
from app.schemas.health import HealthResponse
from app.services.health import get_health_status

router = APIRouter(prefix="/health", tags=["health"])


@router.get("", summary="Check API liveness")
def health_check(settings: SettingsDep) -> HealthResponse:
    """Return a lightweight process liveness response."""

    return get_health_status(settings.app_name)
