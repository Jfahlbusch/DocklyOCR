#!/usr/bin/env python3
"""Delete job data (uploads, pages, results) older than RESULT_TTL_DAYS.

Usage:
    python scripts/cleanup_old_results.py          # dry-run (shows what would be deleted)
    python scripts/cleanup_old_results.py --delete  # actually delete

Intended to run as a daily cron job on the server.
"""

from __future__ import annotations

import argparse
import shutil
import sys
from datetime import datetime, timedelta
from pathlib import Path

from app.config import settings
from app.db import engine
from app.models import Job


def main() -> int:
    parser = argparse.ArgumentParser(description="Cleanup old DocklyOCR job data")
    parser.add_argument("--delete", action="store_true", help="Actually delete (default: dry-run)")
    parser.add_argument("--days", type=int, default=None, help="Override RESULT_TTL_DAYS from .env")
    args = parser.parse_args()

    ttl_days = args.days if args.days is not None else settings.result_ttl_days
    cutoff = datetime.utcnow() - timedelta(days=ttl_days)
    storage_dir = Path(settings.storage_dir)
    mode = "DELETE" if args.delete else "DRY-RUN"

    print(f"DocklyOCR Cleanup — {mode}")
    print(f"  TTL: {ttl_days} days (cutoff: {cutoff.isoformat()})")
    print(f"  Storage: {storage_dir}")
    print()

    from sqlmodel import Session, select

    deleted_jobs = 0
    deleted_bytes = 0

    with Session(engine) as session:
        old_jobs = session.exec(
            select(Job).where(
                Job.created_at < cutoff,
                Job.status.in_(["done", "failed"]),
            )
        ).all()

        if not old_jobs:
            print("No jobs older than cutoff. Nothing to clean.")
            return 0

        print(f"Found {len(old_jobs)} jobs older than {ttl_days} days:")

        for job in old_jobs:
            job_dir = storage_dir / job.id
            dir_size = 0
            if job_dir.exists():
                dir_size = sum(f.stat().st_size for f in job_dir.rglob("*") if f.is_file())

            print(
                f"  {job.id[:12]}... | {job.input_filename or '?':30s} "
                f"| {job.status:10s} | {job.created_at.strftime('%Y-%m-%d %H:%M')} "
                f"| {dir_size / 1024:.0f} KB"
            )

            if args.delete:
                # Delete storage directory
                if job_dir.exists():
                    shutil.rmtree(job_dir)

                # Delete DB row
                session.delete(job)

                deleted_jobs += 1
                deleted_bytes += dir_size

        if args.delete:
            session.commit()

    print()
    if args.delete:
        print(f"Deleted {deleted_jobs} jobs, freed {deleted_bytes / 1024 / 1024:.1f} MB.")
    else:
        print(f"Would delete {len(old_jobs)} jobs. Run with --delete to execute.")

    return 0


if __name__ == "__main__":
    sys.exit(main())
