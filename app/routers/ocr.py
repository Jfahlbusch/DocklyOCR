"""POST /v1/ocr — submit a document for OCR processing.

The endpoint supports two execution modes:

* ``mode=sync`` — runs the 5-strategy pipeline in-process and returns the
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
from app.http_utils import content_disposition_attachment
from app.models import Job, JobStatus, OutputFormat
from app.schemas import ErrorResponse, OcrAsyncResponse
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


_OCR_DESCRIPTION = """
Submit a PDF or image document to the DocklyOCR pipeline and receive the
extracted text in the requested format.

The endpoint supports two execution modes selected via the ``mode`` form
field:

* **``mode=sync``** — runs the 5-strategy pipeline in-process and returns
  the rendered result body directly with the format-appropriate
  ``Content-Type``. Best for small documents where you want to block on
  the result.
* **``mode=async``** *(default)* — enqueues a background job and returns
  ``202 Accepted`` with a ``job_id`` and ``status_url``. Poll
  ``GET /v1/jobs/{job_id}`` or provide a ``webhook_url`` for push
  notification once processing finishes.

**Accepted content types:** ``application/pdf``, ``image/jpeg``,
``image/png``, ``image/tiff``. Other types are rejected with ``415``.

**Upload size limit:** 100 MB per request. Larger bodies are rejected with
``413`` before the pipeline runs.

**Output formats** (``output_format`` form field): ``md`` (Markdown),
``txt`` (plain text), ``toon`` (TOON tree notation), ``json`` (structured
pipeline result). The sync response's ``Content-Type`` reflects the chosen
format.

**Webhook delivery (async mode):** if ``webhook_url`` is supplied, the
worker POSTs the final job payload to that URL on completion. Delivery is
retried with exponential backoff; inspect ``webhook_delivered`` and
``webhook_attempts`` on the job detail endpoint.

**Authentication:** every request must include a valid ``X-API-Key``
header. Requests are rate-limited per API key (see platform configuration
for the current quota).
"""


@router.post(
    "/ocr",
    summary="Submit a document for OCR processing",
    description=_OCR_DESCRIPTION,
    response_model=None,
    responses={
        202: {
            "description": (
                "Async job accepted — poll ``GET /v1/jobs/{job_id}`` or wait for the "
                "configured webhook. Returned when ``mode=async``."
            ),
            "model": OcrAsyncResponse,
            "content": {
                "application/json": {
                    "example": {
                        "job_id": "7c9e6f8d5b2a4e1c9d8f3a6b7e5c2d1a",
                        "status": "pending",
                        "status_url": "/v1/jobs/7c9e6f8d5b2a4e1c9d8f3a6b7e5c2d1a",
                    }
                }
            },
        },
        200: {
            "description": (
                "Sync OCR result. Content type and body shape depend on "
                "``output_format``. Returned when ``mode=sync``."
            ),
            "content": {
                "text/markdown": {"example": "## Seite 1\n\nHello world\n"},
                "text/plain": {"example": "Hello world\n"},
                "application/json": {
                    "example": {
                        "meta": {"page_count": 2, "pages_ok": 2, "pages_failed": 0},
                        "pages": [
                            {"number": 1, "text": "Hello world", "strategy": "150dpi/1024px"},
                            {"number": 2, "text": "Second page", "strategy": "200dpi/1600px"},
                        ],
                    }
                },
                "application/x-toon": {
                    "example": "document:\n  meta:\n    page_count: 2\n  pages:\n    - number: 1\n      text: Hello world\n"
                },
            },
        },
        400: {
            "model": ErrorResponse,
            "description": "Invalid ``output_format`` value.",
            "content": {
                "application/json": {
                    "example": {
                        "detail": "Invalid output_format 'yaml'. Must be one of: md, txt, toon, json"
                    }
                }
            },
        },
        401: {
            "model": ErrorResponse,
            "description": "Missing or invalid ``X-API-Key`` header.",
            "content": {"application/json": {"example": {"detail": "Invalid or inactive API key"}}},
        },
        413: {
            "model": ErrorResponse,
            "description": "Upload exceeds the configured size limit.",
            "content": {
                "application/json": {
                    "example": {"error": "Payload too large", "max_bytes": 104857600}
                }
            },
        },
        415: {
            "model": ErrorResponse,
            "description": "Uploaded file has an unsupported media type.",
            "content": {
                "application/json": {
                    "example": {
                        "detail": (
                            "Unsupported media type 'text/plain'. Allowed: "
                            "['application/pdf', 'image/jpeg', 'image/png', 'image/tiff']"
                        )
                    }
                }
            },
        },
        429: {
            "model": ErrorResponse,
            "description": "Rate limit exceeded for this API key.",
            "content": {"application/json": {"example": {"detail": "Rate limit exceeded"}}},
        },
        503: {
            "model": ErrorResponse,
            "description": "Async queue is unavailable (Redis/ARQ not ready).",
            "content": {"application/json": {"example": {"detail": "Queue unavailable"}}},
        },
    },
)
async def submit_ocr(
    request: Request,
    file: UploadFile = File(...),  # noqa: B008 -- FastAPI dep pattern
    output_format: str = Form(...),
    mode: Literal["sync", "async"] = Form("async"),
    webhook_url: str | None = Form(None),
    sanitize: bool = Form(False),
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
        sanitize=sanitize,
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
    # Sync path runs in-process against ``settings.backend_url`` — there's no
    # GPU-fallback dance here, so the served instance is always the primary.
    job.backend_model = settings.backend_model
    job.backend_instance = settings.scw_gpu_instance_label
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

    # Mirror worker behaviour: a run that finishes without crashing but where
    # every page failed is not a success — the body would be empty and a
    # caller polling the status would see ``done`` + nothing to use.
    if result.page_count > 0 and result.pages_ok == 0:
        job.status = JobStatus.failed
        job.error_message = (
            f"OCR fehlgeschlagen auf allen {result.page_count} Seiten "
            "(Backend hat alle Requests abgelehnt)."
        )
    else:
        job.status = JobStatus.done
    job.finished_at = datetime.utcnow()
    job.page_count = result.page_count
    job.pages_ok = result.pages_ok
    job.pages_failed = result.pages_failed
    job.result_path = str(result_path)
    job.result_mime = mime
    session.add(job)
    session.commit()

    if job.status == JobStatus.failed:
        raise HTTPException(
            status_code=502,
            detail=job.error_message,
        )

    filename = _result_filename(job.input_filename, fmt_enum.value)
    return Response(
        content=body,
        media_type=mime,
        headers={
            "Content-Disposition": content_disposition_attachment(filename),
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


# ---------------------------------------------------------------------------
# Batch upload: multiple files in a single request
# ---------------------------------------------------------------------------


@router.post(
    "/ocr/batch",
    summary="Submit multiple documents for OCR in a single request",
    description=(
        "Upload several files at once (max 50 per request). Each file is "
        "validated independently and becomes its own Job — returned is a "
        "list of job_ids that can be polled individually via /v1/jobs/{id}. "
        "All jobs share the same output_format and webhook_url. Mode is "
        "always async (sync would block for too long on a batch). The GPU "
        "remains on throughout the batch and shuts down automatically "
        "after the final job finishes."
    ),
    status_code=202,
    tags=["ocr"],
    responses={
        202: {
            "description": "Batch accepted — poll /v1/jobs/{id} for each job",
            "content": {
                "application/json": {
                    "example": {
                        "count": 2,
                        "jobs": [
                            {
                                "job_id": "abc123",
                                "filename": "file1.pdf",
                                "status_url": "/v1/jobs/abc123",
                            },
                            {
                                "job_id": "def456",
                                "filename": "file2.pdf",
                                "status_url": "/v1/jobs/def456",
                            },
                        ],
                    }
                }
            },
        },
        400: {"model": ErrorResponse, "description": "No files provided or bad format"},
        401: {"model": ErrorResponse, "description": "Missing or invalid API key"},
        413: {"model": ErrorResponse, "description": "One or more files exceed size limit"},
        415: {"model": ErrorResponse, "description": "Unsupported MIME on one or more files"},
        429: {"model": ErrorResponse, "description": "Rate limit exceeded"},
    },
)
async def submit_ocr_batch(
    request: Request,
    files: list[UploadFile] = File(...),  # noqa: B008
    output_format: str = Form(...),
    webhook_url: str | None = Form(None),
    sanitize: bool = Form(False),
    ctx: ApiKeyContext = Depends(enforce_rate_limit),  # noqa: B008
    session: Session = Depends(get_session),  # noqa: B008
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

    # --- Validate number of files ------------------------------------------
    if not files:
        raise HTTPException(status_code=400, detail="No files provided")
    if len(files) > 50:
        raise HTTPException(
            status_code=400, detail=f"Too many files ({len(files)}). Max 50 per batch."
        )

    # --- Validate every file upfront (fail fast before creating any job) ---
    file_data: list[tuple[UploadFile, bytes, str]] = []
    for f in files:
        mime = (f.content_type or "").split(";")[0].strip().lower()
        if mime not in _ALLOWED_MIMES:
            raise HTTPException(
                status_code=415,
                detail=(
                    f"Unsupported media type '{mime or 'unknown'}' for "
                    f"'{f.filename or 'upload'}'. "
                    f"Allowed: {sorted(_ALLOWED_MIMES)}"
                ),
            )
        if f.size is not None and f.size > settings.max_upload_bytes:
            raise HTTPException(
                status_code=413,
                detail=(
                    f"File '{f.filename or 'upload'}' too large. "
                    f"Max {settings.max_upload_bytes} bytes."
                ),
            )
        data = await f.read()
        if len(data) > settings.max_upload_bytes:
            raise HTTPException(
                status_code=413,
                detail=(
                    f"File '{f.filename or 'upload'}' exceeds size. "
                    f"Max {settings.max_upload_bytes} bytes."
                ),
            )
        file_data.append((f, data, mime))

    # --- Create Jobs + persist uploads -------------------------------------
    assert ctx.api_key.id is not None
    assert ctx.customer.id is not None
    created_jobs: list[Job] = []

    for f, data, mime in file_data:
        job = Job(
            api_key_id=ctx.api_key.id,
            customer_id=ctx.customer.id,
            status=JobStatus.pending,
            input_filename=f.filename or "upload",
            input_size_bytes=len(data),
            input_mime=mime,
            output_format=fmt_enum,
            webhook_url=webhook_url,
            sanitize=sanitize,
        )
        session.add(job)
        session.commit()
        session.refresh(job)

        try:
            storage.save_upload(job.id, f.filename or "upload", data)
        except OSError as e:
            job.status = JobStatus.failed
            job.error_message = f"Failed to persist upload: {e}"
            job.finished_at = datetime.utcnow()
            session.add(job)
            session.commit()
            continue

        created_jobs.append(job)

    # --- Enqueue all jobs on ARQ ------------------------------------------
    pool = await get_arq_pool(request)
    for job in created_jobs:
        await pool.enqueue_job("process_ocr_job", job.id)

    return JSONResponse(
        status_code=202,
        content={
            "count": len(created_jobs),
            "jobs": [
                {
                    "job_id": j.id,
                    "filename": j.input_filename,
                    "status_url": f"/v1/jobs/{j.id}",
                }
                for j in created_jobs
            ],
        },
    )
