#!/usr/bin/env python3
"""Manually delete job data (uploads, pages, results) older than RESULT_TTL_DAYS.

This is the **manual / one-off** entry point. The same logic also runs
automatically every day via the ARQ cron job
``app.workers.ocr_worker.daily_cleanup``.

Usage:
    python scripts/cleanup_old_results.py            # dry-run
    python scripts/cleanup_old_results.py --delete   # actually delete
    python scripts/cleanup_old_results.py --delete --days 14
"""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from app.config import settings  # noqa: E402
from app.services.cleanup import cleanup_old_jobs  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description="Cleanup old DocklyOCR job data")
    parser.add_argument("--delete", action="store_true", help="Actually delete (default: dry-run)")
    parser.add_argument("--days", type=int, default=None, help="Override RESULT_TTL_DAYS from .env")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(message)s")

    ttl = args.days if args.days is not None else settings.result_ttl_days
    mode = "DELETE" if args.delete else "DRY-RUN"
    print(f"DocklyOCR Cleanup — {mode}")
    print(f"  TTL: {ttl} days")
    print(f"  Storage: {settings.storage_dir}")
    print()

    report = cleanup_old_jobs(ttl_days=args.days, dry_run=not args.delete)

    if report.dry_run:
        print(f"Would delete {report.eligible_count} jobs. Run with --delete to execute.")
    else:
        print(
            f"Deleted {report.deleted_count} jobs, freed {report.deleted_bytes / 1024 / 1024:.1f} MB."
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
