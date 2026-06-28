"""
Fiscal Document Intelligence Service
=====================================
Production-grade microservice for automated invoice/receipt extraction.
Designed for accountancy firms processing high document volumes.
"""

import logging
import time
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from app.api.routes import documents, jobs, health
from app.core.config import settings
from app.core.logging import setup_logging

setup_logging()
logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Application lifespan: startup and shutdown logic."""
    logger.info("Starting Fiscal Document Intelligence Service v%s", settings.VERSION)
    yield
    logger.info("Shutting down service")


app = FastAPI(
    title="Fiscal Document Intelligence API",
    description="""
    Automated extraction of structured data from fiscal documents (invoices, receipts).
    
    ## Features
    - Multi-strategy PDF extraction (text layer → AI vision → OCR fallback)
    - Per-field confidence scoring with human-review routing
    - Async job processing — no HTTP timeouts on large documents
    - Full audit trail for compliance
    - Validated structured output (Pydantic schemas) ready for any ERP/database
    """,
    version=settings.VERSION,
    lifespan=lifespan,
    docs_url="/docs",
    redoc_url="/redoc",
)

# ── Middleware ────────────────────────────────────────────────────────────────

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.ALLOWED_ORIGINS,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.middleware("http")
async def add_request_timing(request: Request, call_next):
    """Log request duration for performance monitoring."""
    start = time.perf_counter()
    response = await call_next(request)
    duration_ms = (time.perf_counter() - start) * 1000
    response.headers["X-Process-Time-Ms"] = f"{duration_ms:.1f}"
    logger.debug("%s %s → %d (%.0fms)", request.method, request.url.path, response.status_code, duration_ms)
    return response


@app.exception_handler(Exception)
async def global_exception_handler(request: Request, exc: Exception):
    logger.exception("Unhandled exception on %s %s", request.method, request.url.path)
    return JSONResponse(
        status_code=500,
        content={"detail": "Internal server error. The incident has been logged."},
    )


# ── Routers ───────────────────────────────────────────────────────────────────

app.include_router(health.router, prefix="/health", tags=["Health"])
app.include_router(documents.router, prefix="/api/v1/documents", tags=["Documents"])
app.include_router(jobs.router, prefix="/api/v1/jobs", tags=["Jobs"])
