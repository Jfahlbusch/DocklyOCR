"""ARQ worker for async OCR processing.

The task ``process_ocr_job(job_id)`` runs the OCR pipeline in a subprocess
(via ``app.services.ocr_runner``) so that a segfault inside the backend
HTTP client or ``pdftoppm`` never takes down the worker process — only
the subprocess dies and the task marks the job as ``failed``.

On completion, if a ``webhook_url`` is configured the task attempts one
synchronous delivery via ``deliver_webhook``. On failure (and while we
haven't exceeded the retry cap) a follow-up ``deliver_with_retry`` task is
scheduled with ``_defer_by`` matching the configured backoff sequence.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
from datetime import datetime, timedelta
from pathlib import Path

from arq.connections import RedisSettings
from arq.cron import cron
from sqlmodel import Session

from app.config import settings
from app.db import engine
from app.models import Job, JobStatus
from app.services.cleanup import cleanup_old_jobs
from app.services.document_router import select_engine
from app.services.formatters import format_output
from app.services.gpu_manager import ensure_any_gpu_running
from app.services.ocr_pipeline import OcrResult
from app.services.ocr_runner import EXIT_OPENDATALOADER_UNACCEPTABLE
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

        # Pick the cheap engine first: digital PDFs go to opendataloader
        # on CPU (no GPU spin-up). Images and scanned PDFs go straight
        # to the vLLM pipeline.
        planned_engine = select_engine(input_path)

        try:
            with tempfile.TemporaryDirectory(prefix=f"ocr_{job_id}_") as tmp_dir_str:
                tmp_dir = Path(tmp_dir_str)
                result_json_path = tmp_dir / "result.json"
                # Write incremental output + page images to STORAGE (persistent, visible)
                result_output_path = storage.base_dir / job_id / f"result.{job.output_format.value}"
                pages_dir = storage.base_dir / job_id / "pages"

                # Structure-JSON sidecar lives alongside result.md in storage
                # and is only emitted by the opendataloader engine.
                structure_path = storage.base_dir / job_id / "structure.json"

                def _run_subprocess(engine: str, backend_url: str | None) -> int:
                    cmd = [
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
                        "--engine",
                        engine,
                    ]
                    if engine == "opendataloader":
                        cmd += ["--structure-path", str(structure_path)]
                        if job.sanitize:
                            cmd += ["--sanitize"]
                    env = {**os.environ}
                    if backend_url is not None:
                        env["BACKEND_URL"] = backend_url
                    proc = subprocess.run(cmd, check=False, timeout=_PIPELINE_TIMEOUT_S, env=env)
                    return proc.returncode

                final_engine = planned_engine
                backend_url: str | None = None
                instance_label: str | None = None

                if planned_engine == "opendataloader":
                    # CPU-only — no GPU touch.
                    rc = _run_subprocess("opendataloader", backend_url=None)
                    if rc == EXIT_OPENDATALOADER_UNACCEPTABLE:
                        # The text-layer probe said yes, but the actual
                        # extraction was too sparse → fall back to vllm.
                        try:
                            backend_url, instance_label = ensure_any_gpu_running()
                        except RuntimeError as e:
                            job.status = JobStatus.failed
                            job.error_message = (
                                f"GPU boot failed (after opendataloader fallback): {e}"
                            )
                            job.finished_at = datetime.utcnow()
                            session.add(job)
                            session.commit()
                            return "gpu_boot_timeout"
                        rc = _run_subprocess("vllm", backend_url=backend_url)
                        final_engine = "vllm-fallback-after-opendataloader"
                    if rc != 0:
                        raise subprocess.CalledProcessError(rc, "ocr_runner")
                else:  # vllm path: needs the GPU upfront
                    try:
                        backend_url, instance_label = ensure_any_gpu_running()
                    except RuntimeError as e:
                        job.status = JobStatus.failed
                        job.error_message = f"GPU boot failed: {e}"
                        job.finished_at = datetime.utcnow()
                        session.add(job)
                        session.commit()
                        return "gpu_boot_timeout"
                    rc = _run_subprocess("vllm", backend_url=backend_url)
                    if rc != 0:
                        raise subprocess.CalledProcessError(rc, "ocr_runner")

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

        # Pipeline completed without crashing, but if *every* page failed
        # we must not mark the job as ``done`` — a user polling the status
        # would see success + get an empty result body. Treat it as a
        # real failure so it's visible in the admin UI and retry-able.
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
        # Record which engine actually produced the result. backend_model
        # and backend_instance are only meaningful when the vision-LLM ran.
        job.engine = final_engine
        if final_engine in ("vllm", "vllm-fallback-after-opendataloader"):
            job.backend_model = settings.backend_model
            job.backend_instance = instance_label
        session.add(job)
        session.commit()

        has_webhook = bool(job.webhook_url)

    # Deliver the webhook outside the session/transaction.
    if has_webhook:
        success = await deliver_webhook(job_id)
        if not success:
            await _schedule_webhook_retry(ctx, job_id)

    # GPU shutdown is handled by the GPU-side safety timer (5 min idle).
    # Proactive shutdown from the worker is disabled because of a race:
    # between job N returning and job N+1 being picked up, the queue
    # momentarily appears empty → GPU shutdown would fire → next job
    # would see a dead backend.
    #
    # Trade-off: each batch run leaves the GPU powered on for up to 5
    # minutes after the last job. Acceptable given that cold-start would
    # cost ~3 minutes anyway.
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


async def daily_cleanup(ctx) -> str:  # noqa: ARG001 -- ARQ injects ctx
    """Cron entry point — purge OCR jobs and storage older than the TTL.

    Runs every day at 03:00 UTC. Uses ``settings.result_ttl_days`` (set
    to 7 by default; override via ``RESULT_TTL_DAYS`` env var).
    """
    report = cleanup_old_jobs(dry_run=False)
    return (
        f"deleted={report.deleted_count} freed_mb={report.deleted_bytes / 1024 / 1024:.1f} "
        f"ttl_days={report.ttl_days}"
    )


class WorkerSettings:
    """ARQ worker configuration.

    ``max_jobs = 2``: With the engine router (opendataloader on CPU vs.
    vLLM on GPU) the two engines no longer share a critical resource, so
    we can run two jobs concurrently. Realistic mixes:

    * 1 × opendataloader + 1 × vLLM — no contention; the opendataloader
      worker process is CPU-bound while the vLLM worker is mostly
      waiting on HTTP responses from the remote GPU.
    * 2 × opendataloader — both on CPU; fits 3 vCPUs comfortably.
    * 2 × vLLM — queue inside vLLM; not faster than serial but harmless.

    Within each vLLM job the pipeline still fires MAX_PARALLEL_PAGES (12)
    page requests, so the GPU is well-utilised even at max_jobs=2.
    """

    functions = [process_ocr_job, deliver_with_retry]
    # Daily cleanup at 03:00 UTC (05:00 MESZ) — low-traffic window.
    # ARQ ``unique=True`` (default) ensures only one worker runs it even
    # if multiple workers are scaled up later.
    cron_jobs = [cron(daily_cleanup, hour={3}, minute=0)]
    redis_settings = RedisSettings.from_dsn(settings.redis_url)
    max_jobs = 2
    # Slightly above the subprocess pipeline timeout so the TimeoutExpired
    # branch above runs cleanly before ARQ gives up.
    job_timeout = _PIPELINE_TIMEOUT_S + 300
