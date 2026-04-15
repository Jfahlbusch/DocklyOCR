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
from app.schemas import ErrorResponse, JobDetailResponse, JobListResponse
from app.services.storage import storage

router = APIRouter(tags=["jobs"])


def _job_to_response(job: Job) -> JobDetailResponse:
    """Map a ``Job`` ORM row onto the public ``JobDetailResponse`` schema."""
    result_url: str | None = None
    if job.status == JobStatus.done:
        result_url = f"/v1/jobs/{job.id}/result"
    return JobDetailResponse(
        job_id=job.id,
        status=job.status,
        created_at=job.created_at,
        started_at=job.started_at,
        finished_at=job.finished_at,
        output_format=job.output_format,
        input_filename=job.input_filename,
        page_count=job.page_count,
        pages_ok=job.pages_ok,
        pages_failed=job.pages_failed,
        error_message=job.error_message,
        result_url=result_url,
        webhook_url=job.webhook_url,
        webhook_delivered=job.webhook_delivered,
        webhook_attempts=job.webhook_attempts,
    )


def _result_filename(original: str, extension: str) -> str:
    """Build the result download filename: ``<stem>.<ext>``."""
    stem = Path(original).stem or "result"
    return f"{stem}.{extension}"


_LIST_JOBS_DESCRIPTION = """
Return a paginated list of OCR jobs belonging to the caller's customer,
ordered newest-first by ``created_at``.

Use the ``limit``/``offset`` query parameters to page through results, and
the optional ``status`` filter to narrow by lifecycle state (``pending``,
``processing``, ``done``, or ``failed``).

Jobs belonging to other customers are never returned — each API key is
strictly scoped to its owning tenant.
"""


@router.get(
    "/jobs",
    summary="List your OCR jobs",
    description=_LIST_JOBS_DESCRIPTION,
    response_model=JobListResponse,
    responses={
        200: {
            "description": "Page of jobs matching the query.",
        },
        401: {
            "model": ErrorResponse,
            "description": "Missing or invalid ``X-API-Key`` header.",
            "content": {"application/json": {"example": {"detail": "Invalid or inactive API key"}}},
        },
        422: {
            "model": ErrorResponse,
            "description": "Query parameters failed validation (e.g. ``limit > 100``).",
        },
    },
)
async def list_jobs(
    ctx: ApiKeyContext = Depends(require_api_key),  # noqa: B008
    session: Session = Depends(get_session),  # noqa: B008
    limit: int = Query(  # noqa: B008 -- FastAPI dep pattern
        20,
        ge=1,
        le=100,
        description="Maximum number of items to return (1–100). Defaults to 20.",
    ),
    offset: int = Query(  # noqa: B008 -- FastAPI dep pattern
        0,
        ge=0,
        description="Zero-based index of the first item to return. Defaults to 0.",
    ),
    status: JobStatus | None = Query(  # noqa: B008 -- FastAPI dep pattern
        None,
        description="Optional filter: only return jobs in this lifecycle state.",
    ),
) -> JobListResponse:
    base = select(Job).where(Job.customer_id == ctx.customer.id)
    count_stmt = select(func.count()).select_from(Job).where(Job.customer_id == ctx.customer.id)
    if status is not None:
        base = base.where(Job.status == status)
        count_stmt = count_stmt.where(Job.status == status)

    total = session.exec(count_stmt).one()
    rows = session.exec(base.order_by(Job.created_at.desc()).offset(offset).limit(limit)).all()

    return JobListResponse(
        items=[_job_to_response(j) for j in rows],
        total=int(total),
        limit=limit,
        offset=offset,
    )


_GET_JOB_DESCRIPTION = """
Return the full status record for a single OCR job, including lifecycle
timestamps, page counts, webhook delivery state, and a ``result_url`` once
processing is complete.

Use this endpoint to poll for async job completion. Jobs belonging to a
different customer return ``404`` to avoid leaking their existence.
"""


@router.get(
    "/jobs/{job_id}",
    summary="Get job status and metadata",
    description=_GET_JOB_DESCRIPTION,
    response_model=JobDetailResponse,
    responses={
        200: {"description": "Job record found."},
        401: {
            "model": ErrorResponse,
            "description": "Missing or invalid ``X-API-Key`` header.",
            "content": {"application/json": {"example": {"detail": "Invalid or inactive API key"}}},
        },
        404: {
            "model": ErrorResponse,
            "description": "No job with that id exists for the caller's customer.",
            "content": {"application/json": {"example": {"detail": "Job not found"}}},
        },
    },
)
async def get_job(
    job_id: str,
    ctx: ApiKeyContext = Depends(require_api_key),  # noqa: B008
    session: Session = Depends(get_session),  # noqa: B008
) -> JobDetailResponse:
    job = session.get(Job, job_id)
    if job is None or job.customer_id != ctx.customer.id:
        raise HTTPException(status_code=404, detail="Job not found")
    return _job_to_response(job)


_GET_RESULT_DESCRIPTION = """
Download the rendered result body for a completed OCR job. The response's
``Content-Type`` matches the ``output_format`` the job was submitted with
(``text/markdown``, ``text/plain``, ``application/json``, or
``application/x-toon``).

Returns ``409 Conflict`` if the job is still processing and ``410 Gone``
if the result file has been evicted by the retention policy.
"""


@router.get(
    "/jobs/{job_id}/result",
    summary="Download job result",
    description=_GET_RESULT_DESCRIPTION,
    response_model=None,
    responses={
        200: {
            "description": (
                "Rendered OCR result body. Content type matches the job's ``output_format``."
            ),
            "content": {
                "text/markdown": {"example": "## Seite 1\n\nHello world\n"},
                "text/plain": {"example": "Hello world\n"},
                "application/json": {
                    "example": {
                        "meta": {"page_count": 1, "pages_ok": 1, "pages_failed": 0},
                        "pages": [
                            {
                                "number": 1,
                                "text": "Hello world",
                                "strategy": "150dpi/1024px",
                            }
                        ],
                    }
                },
                "application/x-toon": {
                    "example": "document:\n  meta:\n    page_count: 1\n  pages:\n    - number: 1\n      text: Hello world\n"
                },
            },
        },
        401: {
            "model": ErrorResponse,
            "description": "Missing or invalid ``X-API-Key`` header.",
            "content": {"application/json": {"example": {"detail": "Invalid or inactive API key"}}},
        },
        404: {
            "model": ErrorResponse,
            "description": "No job with that id exists for the caller's customer.",
            "content": {"application/json": {"example": {"detail": "Job not found"}}},
        },
        409: {
            "model": ErrorResponse,
            "description": "Result is not ready yet — the job is still processing.",
            "content": {"application/json": {"example": {"detail": "Result not ready"}}},
        },
        410: {
            "model": ErrorResponse,
            "description": "Result file has been removed (retention window expired).",
            "content": {"application/json": {"example": {"detail": "Result no longer available"}}},
        },
    },
)
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
