"""SQLModel database models for DocklyOCR.

Implements the data model from `OCR-API-Projekt-Anforderungen.md` §3:
- Customer: tenant identity + plan/quota fields
- ApiKey: SHA-256 hashed API keys per customer
- Job: OCR job tracking with status enum
- AdminUser: single-user admin auth (bcrypt)
"""

import uuid
from datetime import datetime
from enum import Enum

from sqlmodel import Field, Relationship, SQLModel

# NOTE: Do **not** add `from __future__ import annotations` to this file.
# SQLModel/SQLAlchemy reads the runtime annotations on `Relationship(...)`
# fields to resolve the related class. With future-annotations all annotations
# become strings (e.g. `'list[ApiKey]'`) which SQLAlchemy then tries to
# resolve as a literal class name in its registry and fails. Keeping native
# annotations here is required for the ORM to wire up relationships.


class JobStatus(str, Enum):  # noqa: UP042 -- spec mandates `(str, Enum)` form
    """Lifecycle states for an OCR job."""

    pending = "pending"
    processing = "processing"
    done = "done"
    failed = "failed"


class OutputFormat(str, Enum):  # noqa: UP042 -- spec mandates `(str, Enum)` form
    """Supported OCR output formats."""

    md = "md"
    txt = "txt"
    toon = "toon"
    json = "json"


def _new_uuid_hex() -> str:
    """Default factory for `Job.id` — 32-char hex without dashes."""
    return uuid.uuid4().hex


class Customer(SQLModel, table=True):
    """Tenant / customer record. API keys and jobs hang off this."""

    id: int | None = Field(default=None, primary_key=True)
    name: str
    email: str = Field(unique=True, index=True)
    created_at: datetime = Field(default_factory=datetime.utcnow)
    is_active: bool = Field(default=True)
    plan: str = Field(default="free")
    monthly_page_limit: int | None = Field(default=None)
    webhook_secret: str | None = Field(default=None)

    api_keys: list["ApiKey"] = Relationship(back_populates="customer")
    jobs: list["Job"] = Relationship(back_populates="customer")


class ApiKey(SQLModel, table=True):
    """SHA-256 hashed API key. Plaintext is never stored."""

    id: int | None = Field(default=None, primary_key=True)
    customer_id: int = Field(foreign_key="customer.id", index=True)
    key_hash: str = Field(unique=True, index=True)
    key_prefix: str
    name: str
    created_at: datetime = Field(default_factory=datetime.utcnow)
    last_used_at: datetime | None = Field(default=None)
    is_active: bool = Field(default=True)

    customer: Customer | None = Relationship(back_populates="api_keys")


class Job(SQLModel, table=True):
    """OCR job tracking record. UUID4 hex as primary key."""

    id: str = Field(default_factory=_new_uuid_hex, primary_key=True)
    api_key_id: int = Field(foreign_key="apikey.id", index=True)
    customer_id: int = Field(foreign_key="customer.id", index=True)
    status: JobStatus = Field(default=JobStatus.pending, index=True)
    input_filename: str
    input_size_bytes: int
    input_mime: str
    output_format: OutputFormat
    webhook_url: str | None = Field(default=None)
    created_at: datetime = Field(default_factory=datetime.utcnow, index=True)
    started_at: datetime | None = Field(default=None)
    finished_at: datetime | None = Field(default=None)
    page_count: int | None = Field(default=None)
    pages_ok: int | None = Field(default=None)
    pages_failed: int | None = Field(default=None)
    error_message: str | None = Field(default=None)
    result_path: str | None = Field(default=None)
    result_mime: str | None = Field(default=None)
    webhook_delivered: bool = Field(default=False)
    webhook_attempts: int = Field(default=0)

    customer: Customer | None = Relationship(back_populates="jobs")


class AdminUser(SQLModel, table=True):
    """Single admin user for the operator console."""

    id: int | None = Field(default=None, primary_key=True)
    username: str = Field(unique=True, index=True)
    password_hash: str
    created_at: datetime = Field(default_factory=datetime.utcnow)
