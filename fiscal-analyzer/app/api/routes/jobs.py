"""Jobs status polling endpoint."""
import datetime
import logging
from fastapi import APIRouter, HTTPException
from app.models.schemas import JobStatus, JobStatusResponse
from app.api.routes.documents import get_job_store

logger = logging.getLogger(__name__)
router = APIRouter()


@router.get(
    "/{job_id}",
    response_model=JobStatusResponse,
    summary="Poll job status",
)
async def get_job_status(job_id: str):
    """
    Poll this endpoint after submitting a document via `/analyze/async`.
    
    - **QUEUED**: waiting in queue
    - **PROCESSING**: extraction in progress  
    - **COMPLETED**: result available in `result` field
    - **FAILED**: check `result.error` for details
    """
    store = get_job_store()
    job = store.get(job_id)

    if not job:
        raise HTTPException(status_code=404, detail=f"Job {job_id} not found")

    progress_messages = {
        JobStatus.QUEUED: "Document is queued for processing",
        JobStatus.PROCESSING: "Extracting data — this may take 10-30 seconds",
        JobStatus.COMPLETED: "Extraction complete",
        JobStatus.FAILED: "Extraction failed",
    }

    return JobStatusResponse(
        job_id=job_id,
        status=job["status"],
        progress_message=progress_messages.get(job["status"]),
        result=job.get("result"),
        created_at=job["created_at"],
        updated_at=job["updated_at"],
    )
