"""ARQ worker for async OCR processing.

The task ``process_ocr_job(job_id)`` runs the 13-strategy pipeline in a
subprocess (via ``app.services.ocr_runner``) so that a segfault inside
Ollama or ``pdftoppm`` never takes down the worker process — only the
subprocess dies and the task marks the job as ``failed``.

On completion, if a ``webhook_url`` is configured the task attempts one
synchronous delivery via ``deliver_webhook``. On failure (and while we
haven't exceeded the retry cap) a follow-up ``deliver_with_retry`` task is
scheduled with ``_defer_by`` matching the configured backoff sequence.
"""

from __future__ import annotations

import json
import subprocess
import sys
import tempfile
from datetime import datetime, timedelta
from pathlib import Path

from arq.connections import RedisSettings
from sqlmodel import Session

from app.config import settings
from app.db import engine
from app.models import Job, JobStatus
from app.services.formatters import format_output
from app.services.ocr_pipeline import OcrResult
from app.services.storage import storage
from app.services.webhook import MAX_ATTEMPTS, RETRY_DELAYS_S, deliver_webhook, deliver_with_retry

# Pipeline subprocess timeout. The ARQ job_timeout sits slightly above this so
# that a subprocess timeout surfaces as ``TimeoutExpired`` (handled cleanly
# below) rather than as ARQ killing the task mid-flight.
_PIPELINE_TIMEOUT_S = 60 * 30  # 30 minutes


async def process_ocr_job(ctx, job_id: str) -> str:
    """ARQ task entry — run the pipeline for a queued job."""
    with Session(engine) as session:
        job = session.get(Job, job_id)
        if job is None:
            return "missing"

        job.status = JobStatus.processing
        job.started_at = datetime.utcnow()
        session.add(job)
        session.commit()
        session.refresh(job)

        input_path = storage.get_input_path(job_id)
        if input_path is None:
            job.status = JobStatus.failed
            job.error_message = "Input file missing from storage"
            job.finished_at = datetime.utcnow()
            session.add(job)
            session.commit()
            return "missing_input"

        try:
            with tempfile.TemporaryDirectory(prefix=f"ocr_{job_id}_") as tmp_dir_str:
                tmp_dir = Path(tmp_dir_str)
                result_json_path = tmp_dir / "result.json"
                # Write incremental output + page images to STORAGE (persistent, visible)
                result_output_path = storage.base_dir / job_id / f"result.{job.output_format.value}"
                pages_dir = storage.base_dir / job_id / "pages"
                subprocess.run(
                    [
                        sys.executable,
                        "-m",
                        "app.services.ocr_runner",
                        "--input",
                        str(input_path),
                        "--tmp-dir",
                        str(tmp_dir / "ocr_work"),
                        "--output-json",
                        str(result_json_path),
                        "--output-path",
                        str(result_output_path),
                        "--output-format",
                        job.output_format.value,
                        "--pages-dir",
                        str(pages_dir),
                    ],
                    check=True,
                    timeout=_PIPELINE_TIMEOUT_S,
                )
                result_data = json.loads(result_json_path.read_text())
                result = OcrResult.from_json_dict(result_data)
        except subprocess.CalledProcessError as e:
            job.status = JobStatus.failed
            job.error_message = f"Pipeline subprocess failed: {e}"
            job.finished_at = datetime.utcnow()
            session.add(job)
            session.commit()
            return "pipeline_failed"
        except subprocess.TimeoutExpired:
            job.status = JobStatus.failed
            job.error_message = f"Pipeline timeout ({_PIPELINE_TIMEOUT_S}s)"
            job.finished_at = datetime.utcnow()
            session.add(job)
            session.commit()
            return "pipeline_timeout"
        except Exception as e:
            job.status = JobStatus.failed
            job.error_message = f"Unexpected error: {type(e).__name__}: {e}"
            job.finished_at = datetime.utcnow()
            session.add(job)
            session.commit()
            return "unexpected_error"

        try:
            body, mime = format_output(result, job.output_format.value)
            result_path = storage.save_result(job_id, body, job.output_format.value)
        except Exception as e:
            job.status = JobStatus.failed
            job.error_message = f"Output formatting/storage failed: {type(e).__name__}: {e}"
            job.finished_at = datetime.utcnow()
            session.add(job)
            session.commit()
            return "format_failed"

        job.status = JobStatus.done
        job.finished_at = datetime.utcnow()
        job.page_count = result.page_count
        job.pages_ok = result.pages_ok
        job.pages_failed = result.pages_failed
        job.result_path = str(result_path)
        job.result_mime = mime
        session.add(job)
        session.commit()

        has_webhook = bool(job.webhook_url)

    # Deliver the webhook outside the session/transaction.
    if has_webhook:
        success = await deliver_webhook(job_id)
        if not success:
            await _schedule_webhook_retry(ctx, job_id)

    return "done"


async def _schedule_webhook_retry(ctx, job_id: str) -> None:
    """Enqueue a deferred retry when attempts remain."""
    with Session(engine) as session:
        job = session.get(Job, job_id)
        if job is None:
            return
        attempts = job.webhook_attempts or 0
        if attempts >= MAX_ATTEMPTS or job.webhook_delivered:
            return
        delay_index = min(attempts, len(RETRY_DELAYS_S) - 1)
        delay_s = RETRY_DELAYS_S[delay_index]

    redis = ctx.get("redis") if isinstance(ctx, dict) else getattr(ctx, "redis", None)
    if redis is None:
        return
    await redis.enqueue_job(
        "deliver_with_retry",
        job_id,
        _defer_by=timedelta(seconds=delay_s),
    )


class WorkerSettings:
    """ARQ worker configuration."""

    functions = [process_ocr_job, deliver_with_retry]
    redis_settings = RedisSettings.from_dsn(settings.redis_url)
    max_jobs = 4
    # Slightly above the subprocess pipeline timeout so the TimeoutExpired
    # branch above runs cleanly before ARQ gives up.
    job_timeout = _PIPELINE_TIMEOUT_S + 300
