"""Pydantic response models for the public DocklyOCR API.

These intentionally live outside ``app/models.py`` (which is SQLModel for
the ORM tables). Keeping plain ``pydantic.BaseModel`` here gives FastAPI
clean JSON schemas for ``/docs`` and ``/redoc`` without leaking ORM
internals into the public OpenAPI spec.

Only the fields that are safe to expose to callers are included.
"""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, Field

from app.models import JobStatus, OutputFormat


class HealthResponse(BaseModel):
    """Combined readiness status of the API and its dependencies."""

    status: str = Field(
        ...,
        examples=["ok"],
        description="Overall health: ``ok`` when every dependency reports healthy, otherwise ``degraded``.",
    )
    backend: str = Field(
        ...,
        examples=["ok"],
        description=(
            "Status of the vLLM OCR backend: ``ok``, ``unreachable``, or ``status_<code>``."
        ),
    )
    db: str = Field(
        ...,
        examples=["ok"],
        description="Status of the configured database (``ok`` or ``unreachable``).",
    )


class ErrorResponse(BaseModel):
    """Standard FastAPI-style error envelope returned for 4xx/5xx responses."""

    detail: str = Field(
        ...,
        examples=["Invalid or inactive API key"],
        description="Human-readable explanation of the failure.",
    )


class OcrAsyncResponse(BaseModel):
    """Body returned when a document is accepted for async processing."""

    job_id: str = Field(
        ...,
        examples=["7c9e6f8d5b2a4e1c9d8f3a6b7e5c2d1a"],
        description="Opaque job identifier (32-char hex). Use it to poll status or fetch the result.",
    )
    status: JobStatus = Field(
        ...,
        examples=["pending"],
        description="Initial job status — always ``pending`` immediately after submission.",
    )
    status_url: str = Field(
        ...,
        examples=["/v1/jobs/7c9e6f8d5b2a4e1c9d8f3a6b7e5c2d1a"],
        description="Relative URL for ``GET /v1/jobs/{job_id}`` to poll status.",
    )


class JobDetailResponse(BaseModel):
    """Detailed status record for a single OCR job."""

    job_id: str = Field(
        ...,
        examples=["7c9e6f8d5b2a4e1c9d8f3a6b7e5c2d1a"],
        description="Opaque job identifier (32-char hex).",
    )
    status: JobStatus = Field(
        ...,
        examples=["done"],
        description="Current lifecycle state of the job.",
    )
    created_at: datetime = Field(
        ...,
        description="UTC timestamp the job was accepted by the API.",
    )
    started_at: datetime | None = Field(
        default=None,
        description="UTC timestamp the worker picked up the job. ``null`` while pending.",
    )
    finished_at: datetime | None = Field(
        default=None,
        description="UTC timestamp the job reached ``done`` or ``failed``. ``null`` until then.",
    )
    output_format: OutputFormat = Field(
        ...,
        examples=["md"],
        description="Requested output format for the result body.",
    )
    input_filename: str | None = Field(
        default=None,
        examples=["invoice.pdf"],
        description="Original filename reported by the uploader (informational only).",
    )
    page_count: int | None = Field(
        default=None,
        examples=[3],
        description="Number of pages detected in the source document.",
    )
    pages_ok: int | None = Field(
        default=None,
        examples=[3],
        description="Number of pages the pipeline processed successfully.",
    )
    pages_failed: int | None = Field(
        default=None,
        examples=[0],
        description="Number of pages the pipeline failed on after all fallback strategies.",
    )
    error_message: str | None = Field(
        default=None,
        description="Populated when ``status`` is ``failed``.",
    )
    backend_model: str | None = Field(
        default=None,
        examples=["qwen2.5-vl-7b"],
        description="Vision-LLM model that served this job. ``null`` for jobs created before tracking was added.",
    )
    backend_instance: str | None = Field(
        default=None,
        examples=["H100-1-80G"],
        description="Operator-friendly hardware label of the GPU that served this job.",
    )
    result_url: str | None = Field(
        default=None,
        examples=["/v1/jobs/7c9e6f8d5b2a4e1c9d8f3a6b7e5c2d1a/result"],
        description="Relative URL for downloading the result body. Only set when ``status`` is ``done``.",
    )
    webhook_url: str | None = Field(
        default=None,
        description="Webhook URL that will receive a POST on completion. ``null`` if not configured.",
    )
    webhook_delivered: bool = Field(
        default=False,
        description="Whether the completion webhook has been successfully delivered.",
    )
    webhook_attempts: int = Field(
        default=0,
        description="Number of webhook delivery attempts made so far.",
    )


class JobListResponse(BaseModel):
    """Paginated list of jobs belonging to the caller's customer."""

    items: list[JobDetailResponse] = Field(
        ...,
        description="Page of job records ordered newest-first by ``created_at``.",
    )
    total: int = Field(
        ...,
        examples=[42],
        description="Total number of jobs matching the query (before ``limit``/``offset``).",
    )
    limit: int = Field(
        ...,
        examples=[20],
        description="Maximum number of items returned in this page.",
    )
    offset: int = Field(
        ...,
        examples=[0],
        description="Zero-based index of the first item returned.",
    )
