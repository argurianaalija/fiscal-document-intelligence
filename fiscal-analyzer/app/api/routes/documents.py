"""
Document API Routes
===================
Endpoints for submitting fiscal documents for extraction.

POST /api/v1/documents/analyze        — synchronous (small docs, <5 pages)
POST /api/v1/documents/analyze/async  — async job (recommended for production)
"""

from __future__ import annotations

import logging
import time
import uuid
from pathlib import Path
from typing import Annotated

from fastapi import APIRouter, Depends, File, HTTPException, Security, UploadFile, status

from app.core.config import settings
from app.core.security import verify_api_key
from app.models.schemas import (
    ExtractionResult,
    JobStatus,
    ReviewStatus,
    SubmitDocumentResponse,
)
from app.services.extraction_engine import ExtractionOrchestrator

logger = logging.getLogger(__name__)
router = APIRouter()
orchestrator = ExtractionOrchestrator()

# In-memory job store (replace with Redis in production — see services/job_queue.py)
_job_store: dict[str, dict] = {}


def _validate_upload(file: UploadFile) -> None:
    """Validate file type and size before processing."""
    if file.content_type not in settings.ALLOWED_MIME_TYPES:
        raise HTTPException(
            status_code=status.HTTP_415_UNSUPPORTED_MEDIA_TYPE,
            detail=f"Unsupported file type: {file.content_type}. Accepted: {settings.ALLOWED_MIME_TYPES}",
        )


async def _save_temp_file(file: UploadFile) -> Path:
    """Save uploaded file to temp location, returning its path."""
    import tempfile

    content = await file.read()

    if len(content) > settings.max_file_size_bytes:
        raise HTTPException(
            status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
            detail=f"File too large: {len(content) / 1024 / 1024:.1f}MB. Max: {settings.MAX_FILE_SIZE_MB}MB",
        )

    suffix = Path(file.filename or "doc").suffix or ".pdf"
    tmp = tempfile.NamedTemporaryFile(delete=False, suffix=suffix)
    tmp.write(content)
    tmp.close()
    return Path(tmp.name)


@router.post(
    "/analyze",
    response_model=ExtractionResult,
    summary="Analyze a fiscal document (synchronous)",
    description="""
    Upload a PDF invoice or receipt and get structured extracted data back immediately.
    
    **Best for:** Small documents (1-5 pages), development/testing.  
    **For production:** Use `/analyze/async` to avoid HTTP timeouts on large documents.
    
    Extraction uses a multi-tier pipeline:
    1. PDF text layer (fast, accurate)
    2. Claude AI vision (for scanned/complex docs)
    3. OCR fallback (last resort)
    """,
    responses={
        200: {"description": "Extraction successful"},
        415: {"description": "Unsupported file type"},
        413: {"description": "File too large"},
        422: {"description": "Processing failed"},
        401: {"description": "Invalid or missing API key"},
    }
)
async def analyze_document_sync(
    file: UploadFile = File(..., description="PDF, JPEG, PNG, or TIFF document"),
    _: str = Security(verify_api_key),
):
    _validate_upload(file)
    tmp_path = await _save_temp_file(file)

    start = time.perf_counter()
    try:
        invoice_data, strategy, page_count, tokens = orchestrator.process(tmp_path)
    except RuntimeError as e:
        raise HTTPException(status_code=422, detail=str(e))
    finally:
        tmp_path.unlink(missing_ok=True)

    duration_ms = (time.perf_counter() - start) * 1000
    confidence = invoice_data.overall_confidence()
    review_fields = invoice_data.fields_needing_review()

    review_status = (
        ReviewStatus.AUTO_APPROVED
        if confidence >= settings.CONFIDENCE_THRESHOLD and not review_fields
        else ReviewStatus.NEEDS_REVIEW
    )

    logger.info(
        "Document '%s' processed: strategy=%s confidence=%.2f review=%s tokens=%d time=%.0fms",
        file.filename, strategy.value, confidence, review_status.value, tokens, duration_ms,
    )

    return ExtractionResult(
        job_id=str(uuid.uuid4()),
        status=JobStatus.COMPLETED,
        document_name=file.filename or "unknown",
        extraction_strategy=strategy,
        page_count=page_count,
        processing_time_ms=duration_ms,
        overall_confidence=confidence,
        review_status=review_status,
        fields_needing_review=review_fields,
        data=invoice_data,
        model_used=settings.AI_MODEL,
        tokens_used=tokens,
    )


@router.post(
    "/analyze/async",
    response_model=SubmitDocumentResponse,
    status_code=status.HTTP_202_ACCEPTED,
    summary="Submit document for async processing (recommended)",
    description="""
    Submit a document and receive a job_id immediately.
    Poll `GET /api/v1/jobs/{job_id}` to retrieve the result.
    
    **Recommended for:** Production workloads, multi-page documents, batch processing.
    """,
)
async def analyze_document_async(
    file: UploadFile = File(...),
    _: str = Security(verify_api_key),
):
    _validate_upload(file)
    tmp_path = await _save_temp_file(file)
    job_id = str(uuid.uuid4())

    # Store job metadata
    import datetime
    _job_store[job_id] = {
        "status": JobStatus.QUEUED,
        "document_name": file.filename,
        "tmp_path": str(tmp_path),
        "created_at": datetime.datetime.utcnow(),
        "updated_at": datetime.datetime.utcnow(),
        "result": None,
        "error": None,
    }

    # In production: push to Redis queue via Celery/ARQ/RQ
    # For this implementation: run as background task
    import asyncio
    asyncio.create_task(_process_job(job_id, tmp_path, file.filename or "unknown"))

    return SubmitDocumentResponse(
        job_id=job_id,
        status=JobStatus.QUEUED,
        message=f"Document '{file.filename}' queued. Poll /api/v1/jobs/{job_id} for results.",
    )


async def _process_job(job_id: str, tmp_path: Path, document_name: str) -> None:
    """Background task that runs extraction and updates job store."""
    import asyncio
    import datetime

    _job_store[job_id]["status"] = JobStatus.PROCESSING
    _job_store[job_id]["updated_at"] = datetime.datetime.utcnow()

    start = time.perf_counter()
    try:
        # Run sync extraction in thread pool to avoid blocking event loop
        loop = asyncio.get_event_loop()
        invoice_data, strategy, page_count, tokens = await loop.run_in_executor(
            None, orchestrator.process, tmp_path
        )

        duration_ms = (time.perf_counter() - start) * 1000
        confidence = invoice_data.overall_confidence()
        review_fields = invoice_data.fields_needing_review()

        review_status = (
            ReviewStatus.AUTO_APPROVED
            if confidence >= settings.CONFIDENCE_THRESHOLD and not review_fields
            else ReviewStatus.NEEDS_REVIEW
        )

        result = ExtractionResult(
            job_id=job_id,
            status=JobStatus.COMPLETED,
            document_name=document_name,
            extraction_strategy=strategy,
            page_count=page_count,
            processing_time_ms=duration_ms,
            overall_confidence=confidence,
            review_status=review_status,
            fields_needing_review=review_fields,
            data=invoice_data,
            model_used=settings.AI_MODEL,
            tokens_used=tokens,
        )

        _job_store[job_id].update({
            "status": JobStatus.COMPLETED,
            "result": result,
            "updated_at": datetime.datetime.utcnow(),
        })

    except Exception as e:
        logger.exception("Job %s failed", job_id)
        _job_store[job_id].update({
            "status": JobStatus.FAILED,
            "error": str(e),
            "updated_at": datetime.datetime.utcnow(),
        })
    finally:
        tmp_path.unlink(missing_ok=True)


# Export job store so the jobs router can access it
def get_job_store() -> dict:
    return _job_store
