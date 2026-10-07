"""FastAPI application entrypoint."""

from fastapi import FastAPI

from app import __version__
from app.api.health import router as health_router
from app.core.config import settings

app = FastAPI(
    title=settings.app_name,
    version=__version__,
    description="Backend API for the Agent Template.",
)
app.include_router(health_router, prefix=settings.api_v1_prefix)
