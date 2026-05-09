"""TTL-based cleanup of old OCR jobs and their on-disk artefacts.

Single source of truth for both the manual CLI script
(``scripts/cleanup_old_results.py``) and the daily ARQ cron job in
``app.workers.ocr_worker``.

A job is eligible for deletion when **all** of the following are true:

* ``Job.created_at < utcnow() - ttl_days``
* ``Job.status`` is ``done`` or ``failed`` (never delete in-flight jobs)

The cleanup removes:

1. The on-disk job folder under ``settings.storage_dir`` (input file,
   per-page images, result file)
2. The ``Job`` row in the database
"""

from __future__ import annotations

import logging
import shutil
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path

from sqlalchemy import select
from sqlmodel import Session

from app.config import settings
from app.db import engine
from app.models import Job, JobStatus

logger = logging.getLogger(__name__)


@dataclass
class CleanupReport:
    """Summary of a cleanup pass — used by the CLI for human output and
    by the cron job for log lines."""

    ttl_days: int
    cutoff: datetime
    eligible_count: int
    deleted_count: int
    deleted_bytes: int
    dry_run: bool


def _job_dir_size(path: Path) -> int:
    if not path.exists():
        return 0
    return sum(f.stat().st_size for f in path.rglob("*") if f.is_file())


def cleanup_old_jobs(ttl_days: int | None = None, dry_run: bool = False) -> CleanupReport:
    """Delete jobs older than ``ttl_days`` plus their storage folder.

    Args:
        ttl_days: Override for ``settings.result_ttl_days``. Pass ``None``
            to use the configured default.
        dry_run: When True, list what would be deleted but don't touch
            disk or database.

    Returns:
        ``CleanupReport`` with counts + bytes freed.
    """
    effective_ttl = ttl_days if ttl_days is not None else settings.result_ttl_days
    cutoff = datetime.utcnow() - timedelta(days=effective_ttl)
    storage_dir = Path(settings.storage_dir)

    deleted_count = 0
    deleted_bytes = 0

    with Session(engine) as session:
        old_jobs = list(
            session.exec(
                select(Job).where(
                    Job.created_at < cutoff,
                    Job.status.in_([JobStatus.done, JobStatus.failed]),
                )
            ).scalars()
        )

        for job in old_jobs:
            job_dir = storage_dir / job.id
            dir_size = _job_dir_size(job_dir)

            if dry_run:
                logger.info(
                    "DRY-RUN would delete job %s (%s, %.1f KB)",
                    job.id[:12],
                    job.input_filename or "?",
                    dir_size / 1024,
                )
                continue

            try:
                if job_dir.exists():
                    shutil.rmtree(job_dir)
                session.delete(job)
                deleted_count += 1
                deleted_bytes += dir_size
            except Exception as e:
                logger.warning("cleanup of job %s failed: %s — skipping", job.id[:12], e)

        if not dry_run and deleted_count > 0:
            session.commit()

    if dry_run:
        logger.info(
            "cleanup dry-run: %d jobs match TTL=%dd cutoff=%s",
            len(old_jobs),
            effective_ttl,
            cutoff.isoformat(timespec="seconds"),
        )
    else:
        logger.info(
            "cleanup deleted %d jobs (%.1f MB freed); TTL=%dd cutoff=%s",
            deleted_count,
            deleted_bytes / 1024 / 1024,
            effective_ttl,
            cutoff.isoformat(timespec="seconds"),
        )

    return CleanupReport(
        ttl_days=effective_ttl,
        cutoff=cutoff,
        eligible_count=len(old_jobs),
        deleted_count=deleted_count,
        deleted_bytes=deleted_bytes,
        dry_run=dry_run,
    )
