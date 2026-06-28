"""Health check endpoint."""
import platform
import sys
from fastapi import APIRouter
from app.core.config import settings

router = APIRouter()

@router.get("/", summary="Health check")
async def health():
    return {
        "status": "healthy",
        "version": settings.VERSION,
        "environment": settings.ENVIRONMENT,
        "python": sys.version.split()[0],
        "platform": platform.system(),
    }

@router.get("/ready", summary="Readiness check")
async def ready():
    """Check if the service has all dependencies available."""
    checks = {}
    
    # Check Anthropic API key configured
    checks["anthropic_api_key"] = bool(settings.ANTHROPIC_API_KEY)
    
    all_ready = all(checks.values())
    return {
        "ready": all_ready,
        "checks": checks,
    }
