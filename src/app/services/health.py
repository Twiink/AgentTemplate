"""Health-check service logic."""

from app.schemas.health import HealthResponse


def get_health_status(app_name: str) -> HealthResponse:
    """Build the public liveness response."""

    return HealthResponse(status="ok", app=app_name)
