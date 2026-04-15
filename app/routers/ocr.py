"""POST /v1/ocr — submit a document for OCR processing.

The endpoint supports two execution modes:

* ``mode=sync`` — runs the 13-strategy pipeline in-process and returns the
  formatted result body directly with the appropriate ``Content-Type``.
* ``mode=async`` (default) — enqueues an ARQ job and returns ``202 Accepted``
  with the job id + status polling URL. The caller is expected to poll
  ``GET /v1/jobs/{id}`` or set a ``webhook_url`` for push notification.
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Literal

from fastapi import APIRouter, Depends, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import JSONResponse, Response
from sqlmodel import Session

from app.auth import ApiKeyContext, enforce_rate_limit
from app.config import settings
from app.db import get_session
from app.models import Job, JobStatus, OutputFormat
from app.services.formatters import format_output
from app.services.ocr_pipeline import run_ocr
from app.services.storage import storage

router = APIRouter(tags=["ocr"])


_ALLOWED_MIMES: set[str] = {
    "application/pdf",
    "image/jpeg",
    "image/png",
    "image/tiff",
}


async def get_arq_pool(request: Request):
    """Return the ARQ Redis pool stashed on ``app.state``.

    Wired at app startup via a lifespan hook in ``app/main.py``. Tests inject
    a stub pool via ``app.dependency_overrides`` or by setting
    ``app.state.arq_pool`` directly on the test FastAPI instance.
    """
    pool = getattr(request.app.state, "arq_pool", None)
    if pool is None:
        raise HTTPException(status_code=503, detail="Queue unavailable")
    return pool


def _result_filename(original: str, extension: str) -> str:
    """Build the result download filename: ``<stem>.<ext>``."""
    stem = Path(original).stem or "result"
    return f"{stem}.{extension}"


@router.post("/ocr", summary="Submit a file for OCR processing")
async def submit_ocr(
    request: Request,
    file: UploadFile = File(...),  # noqa: B008 -- FastAPI dep pattern
    output_format: str = Form(...),
    mode: Literal["sync", "async"] = Form("async"),
    webhook_url: str | None = Form(None),
    ctx: ApiKeyContext = Depends(enforce_rate_limit),  # noqa: B008 -- FastAPI dep pattern
    session: Session = Depends(get_session),  # noqa: B008 -- FastAPI dep pattern
):
    # --- Validate output_format --------------------------------------------
    try:
        fmt_enum = OutputFormat(output_format)
    except ValueError:
        valid = ", ".join(f.value for f in OutputFormat)
        raise HTTPException(
            status_code=400,
            detail=f"Invalid output_format '{output_format}'. Must be one of: {valid}",
        ) from None

    # --- Validate MIME whitelist -------------------------------------------
    mime = (file.content_type or "").split(";")[0].strip().lower()
    if mime not in _ALLOWED_MIMES:
        raise HTTPException(
            status_code=415,
            detail=(
                f"Unsupported media type '{mime or 'unknown'}'. Allowed: {sorted(_ALLOWED_MIMES)}"
            ),
        )

    # --- Cheap early size check (middleware already catches Content-Length)
    if file.size is not None and file.size > settings.max_upload_bytes:
        raise HTTPException(
            status_code=413,
            detail=f"Payload too large. Max {settings.max_upload_bytes} bytes.",
        )

    data = await file.read()
    if len(data) > settings.max_upload_bytes:
        raise HTTPException(
            status_code=413,
            detail=f"Payload too large. Max {settings.max_upload_bytes} bytes.",
        )

    assert ctx.api_key.id is not None
    assert ctx.customer.id is not None

    # --- Create Job row ----------------------------------------------------
    job = Job(
        api_key_id=ctx.api_key.id,
        customer_id=ctx.customer.id,
        status=JobStatus.pending,
        input_filename=file.filename or "upload",
        input_size_bytes=len(data),
        input_mime=mime,
        output_format=fmt_enum,
        webhook_url=webhook_url,
    )
    session.add(job)
    session.commit()
    session.refresh(job)

    # --- Persist upload to storage -----------------------------------------
    try:
        storage.save_upload(job.id, file.filename or "upload", data)
    except OSError as e:
        job.status = JobStatus.failed
        job.error_message = f"Failed to persist upload: {e}"
        job.finished_at = datetime.utcnow()
        session.add(job)
        session.commit()
        raise HTTPException(status_code=500, detail="Storage error") from e

    # --- Dispatch ----------------------------------------------------------
    if mode == "sync":
        return await _run_sync(session, job, fmt_enum)

    return await _run_async(request, job)


async def _run_sync(session: Session, job: Job, fmt_enum: OutputFormat) -> Response:
    """Execute the pipeline in-process and return the rendered body."""
    input_path = storage.get_input_path(job.id)
    if input_path is None:
        raise HTTPException(status_code=500, detail="Input file missing after save")

    tmp_dir = Path("/tmp") / f"ocr_{job.id}"
    tmp_dir.mkdir(parents=True, exist_ok=True)

    job.status = JobStatus.processing
    job.started_at = datetime.utcnow()
    session.add(job)
    session.commit()
    session.refresh(job)

    try:
        result = run_ocr(input_path, tmp_dir=tmp_dir)
    except Exception as e:
        job.status = JobStatus.failed
        job.error_message = f"Pipeline error: {type(e).__name__}: {e}"
        job.finished_at = datetime.utcnow()
        session.add(job)
        session.commit()
        raise HTTPException(status_code=500, detail="OCR pipeline failed") from e

    try:
        body, mime = format_output(result, fmt_enum.value)
    except Exception as e:
        job.status = JobStatus.failed
        job.error_message = f"Format error: {type(e).__name__}: {e}"
        job.finished_at = datetime.utcnow()
        session.add(job)
        session.commit()
        raise HTTPException(status_code=500, detail="Output formatting failed") from e

    result_path = storage.save_result(job.id, body, fmt_enum.value)

    job.status = JobStatus.done
    job.finished_at = datetime.utcnow()
    job.page_count = result.page_count
    job.pages_ok = result.pages_ok
    job.pages_failed = result.pages_failed
    job.result_path = str(result_path)
    job.result_mime = mime
    session.add(job)
    session.commit()

    filename = _result_filename(job.input_filename, fmt_enum.value)
    return Response(
        content=body,
        media_type=mime,
        headers={
            "Content-Disposition": f'attachment; filename="{filename}"',
            "X-Job-ID": job.id,
        },
    )


async def _run_async(request: Request, job: Job) -> JSONResponse:
    """Enqueue the job on ARQ and return a 202 with the status URL."""
    pool = await get_arq_pool(request)
    await pool.enqueue_job("process_ocr_job", job.id)

    return JSONResponse(
        status_code=202,
        content={
            "job_id": job.id,
            "status": JobStatus.pending.value,
            "status_url": f"/v1/jobs/{job.id}",
        },
    )
