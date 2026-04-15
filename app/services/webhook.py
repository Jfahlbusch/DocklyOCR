"""Outbound webhook delivery for completed OCR jobs.

On success (2xx response) the job's ``webhook_delivered`` flag is set and the
attempt counter incremented. Failures do NOT raise — the caller (worker task)
is responsible for enqueueing retries with ``deliver_with_retry`` while
``webhook_attempts < MAX_ATTEMPTS``.

The HMAC payload is signed with the customer's optional ``webhook_secret``
(``X-Signature: sha256=<hex>``). If no secret is configured, no signature
header is sent.
"""

from __future__ import annotations

import hashlib
import hmac
import json

import httpx
from sqlmodel import Session

from app.db import engine
from app.models import Customer, Job

# Retry delays in seconds: 30s, 2min, 10min.
RETRY_DELAYS_S: list[int] = [30, 120, 600]
MAX_ATTEMPTS: int = 3

# Module-level async client factory — tests monkey-patch this to inject a
# ``httpx.MockTransport`` without having to alter the delivery code path.
_client_factory = None


def _make_client() -> httpx.AsyncClient:
    if _client_factory is not None:
        return _client_factory()
    return httpx.AsyncClient(timeout=15.0)


def _build_payload(job: Job) -> bytes:
    payload = {
        "job_id": job.id,
        "status": job.status.value if hasattr(job.status, "value") else job.status,
        "output_format": (
            job.output_format.value if hasattr(job.output_format, "value") else job.output_format
        ),
        "page_count": job.page_count,
        "pages_ok": job.pages_ok,
        "pages_failed": job.pages_failed,
        "result_url": f"/v1/jobs/{job.id}/result",
        "finished_at": (job.finished_at.isoformat() + "Z") if job.finished_at else None,
    }
    return json.dumps(payload, ensure_ascii=False).encode("utf-8")


def _sign(secret: str, body: bytes) -> str:
    return hmac.new(secret.encode("utf-8"), body, hashlib.sha256).hexdigest()


async def deliver_webhook(job_id: str) -> bool:
    """Attempt one webhook delivery for ``job_id``.

    Returns ``True`` on 2xx, ``False`` on any network / non-2xx failure.
    Persists ``webhook_attempts`` and ``webhook_delivered`` to the DB either way.
    """
    with Session(engine) as session:
        job = session.get(Job, job_id)
        if job is None or not job.webhook_url:
            return False
        customer = session.get(Customer, job.customer_id)
        if customer is None:
            return False

        body = _build_payload(job)

        headers = {
            "Content-Type": "application/json",
            "User-Agent": "ocr-api-webhook/1.0",
        }
        if customer.webhook_secret:
            headers["X-Signature"] = f"sha256={_sign(customer.webhook_secret, body)}"

        success = False
        try:
            async with _make_client() as client:
                response = await client.post(job.webhook_url, content=body, headers=headers)
                success = 200 <= response.status_code < 300
        except httpx.HTTPError:
            success = False

        job.webhook_attempts = (job.webhook_attempts or 0) + 1
        if success:
            job.webhook_delivered = True
        session.add(job)
        session.commit()
        return success


async def deliver_with_retry(ctx, job_id: str) -> bool:  # noqa: ARG001
    """ARQ-deferred retry variant.

    ``ctx`` is the ARQ job context (unused here, but part of the ARQ task
    signature). Returns the same truthy/falsy result as ``deliver_webhook``.
    """
    return await deliver_webhook(job_id)
