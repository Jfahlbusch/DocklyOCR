"""GET /v1/jobs, GET /v1/jobs/{id}, GET /v1/jobs/{id}/result.

All endpoints are scoped to the caller's customer — jobs belonging to a
different tenant return ``404`` (not ``403``, to avoid leaking existence).
"""

from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import FileResponse
from sqlmodel import Session, func, select

from app.auth import ApiKeyContext, require_api_key
from app.db import get_session
from app.models import Job, JobStatus
from app.services.storage import storage

router = APIRouter(tags=["jobs"])


def _serialize_job(job: Job) -> dict:
    """Return the JSON-friendly representation of a job."""
    result_url: str | None = None
    if job.status == JobStatus.done:
        result_url = f"/v1/jobs/{job.id}/result"
    return {
        "job_id": job.id,
        "status": job.status.value if hasattr(job.status, "value") else job.status,
        "created_at": job.created_at.isoformat() if job.created_at else None,
        "started_at": job.started_at.isoformat() if job.started_at else None,
        "finished_at": job.finished_at.isoformat() if job.finished_at else None,
        "output_format": (
            job.output_format.value if hasattr(job.output_format, "value") else job.output_format
        ),
        "page_count": job.page_count,
        "pages_ok": job.pages_ok,
        "pages_failed": job.pages_failed,
        "error_message": job.error_message,
        "result_url": result_url,
    }


def _result_filename(original: str, extension: str) -> str:
    stem = Path(original).stem or "result"
    return f"{stem}.{extension}"


@router.get("/jobs", summary="List caller's jobs (paginated)")
async def list_jobs(
    ctx: ApiKeyContext = Depends(require_api_key),  # noqa: B008
    session: Session = Depends(get_session),  # noqa: B008
    limit: int = Query(20, ge=1, le=100),  # noqa: B008 -- FastAPI dep pattern
    offset: int = Query(0, ge=0),  # noqa: B008 -- FastAPI dep pattern
    status: JobStatus | None = Query(None),  # noqa: B008 -- FastAPI dep pattern
) -> dict:
    base = select(Job).where(Job.customer_id == ctx.customer.id)
    count_stmt = select(func.count()).select_from(Job).where(Job.customer_id == ctx.customer.id)
    if status is not None:
        base = base.where(Job.status == status)
        count_stmt = count_stmt.where(Job.status == status)

    total = session.exec(count_stmt).one()
    rows = session.exec(base.order_by(Job.created_at.desc()).offset(offset).limit(limit)).all()

    return {
        "items": [_serialize_job(j) for j in rows],
        "total": int(total),
        "limit": limit,
        "offset": offset,
    }


@router.get("/jobs/{job_id}", summary="Job status + metadata")
async def get_job(
    job_id: str,
    ctx: ApiKeyContext = Depends(require_api_key),  # noqa: B008
    session: Session = Depends(get_session),  # noqa: B008
) -> dict:
    job = session.get(Job, job_id)
    if job is None or job.customer_id != ctx.customer.id:
        raise HTTPException(status_code=404, detail="Job not found")
    return _serialize_job(job)


@router.get("/jobs/{job_id}/result", summary="Download formatted result")
async def get_job_result(
    job_id: str,
    ctx: ApiKeyContext = Depends(require_api_key),  # noqa: B008
    session: Session = Depends(get_session),  # noqa: B008
):
    job = session.get(Job, job_id)
    if job is None or job.customer_id != ctx.customer.id:
        raise HTTPException(status_code=404, detail="Job not found")

    if job.status != JobStatus.done:
        raise HTTPException(status_code=409, detail="Result not ready")

    result_path = storage.get_result_path(job_id)
    if result_path is None or not result_path.exists():
        raise HTTPException(status_code=410, detail="Result no longer available")

    ext = job.output_format.value if hasattr(job.output_format, "value") else str(job.output_format)
    filename = _result_filename(job.input_filename, ext)
    media_type = job.result_mime or "application/octet-stream"

    return FileResponse(
        path=str(result_path),
        media_type=media_type,
        filename=filename,
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )
