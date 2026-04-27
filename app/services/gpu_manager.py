"""On-demand GPU management via Scaleway API.

Called by the ARQ worker before each OCR job. Supports a primary GPU plus
an optional fallback that is tried when the primary returns ``out_of_stock``
(a recurring scenario for H100 in Scaleway). Already-running GPUs are
detected via a quick health ping — no Scaleway API call needed.

Requires these settings for the primary (optional — no-op if empty):

    scw_access_key      — Scaleway API access key
    scw_secret_key      — Scaleway API secret key
    scw_gpu_server_id   — Instance UUID of the primary GPU server
    scw_gpu_zone        — Zone (e.g. fr-par-2)

Optional fallback (both must be set together):

    scw_gpu_server_id_fallback  — Instance UUID of the fallback GPU
    backend_url_fallback        — vLLM URL of the fallback GPU

If the primary lacks a server_id the module is a no-op (caller is expected
to start the backend).
"""

from __future__ import annotations

import logging
import time
from typing import Literal

import httpx

from app.config import settings

logger = logging.getLogger(__name__)

_SCW_API_BASE = "https://api.scaleway.com/instance/v1"
_BOOT_TIMEOUT_SECONDS = 600  # 10 min: GPU boot (20s) + vLLM load + CUDA graph compile

PowerOnResult = Literal["ok", "out_of_stock", "error"]

# 8x8 white JPEG — smallest payload that exercises the full vision pipeline.
# Used by _backend_serves_inference() as a real warmup smoke-test so we don't
# return while vLLM still reports /v1/models=200 but answers 500 to real
# image requests (CUDA graph compile in progress).
_SMOKE_JPEG_B64 = (
    "/9j/4AAQSkZJRgABAQAAAQABAAD/2wBDAAoHBwgHBgoICAgLCgoLDhgQDg0NDh0VFhEYIx8"
    "lJCIfIiEmKzcvJik0KSEiMEExNDk7Pj4+JS5ESUM8SDc9Pjv/2wBDAQoLCw4NDhwQEBw7K"
    "CIoOzs7Ozs7Ozs7Ozs7Ozs7Ozs7Ozs7Ozs7Ozs7Ozs7Ozs7Ozs7Ozs7Ozs7Ozs7Ozs7Ozv/"
    "wAARCAAIAAgDASIAAhEBAxEB/8QAHwAAAQUBAQEBAQEAAAAAAAAAAAECAwQFBgcICQoL/8Q"
    "AtRAAAgEDAwIEAwUFBAQAAAF9AQIDAAQRBRIhMUEGE1FhByJxFDKBkaEII0KxwRVS0fAkM"
    "2JyggkKFhcYGRolJicoKSo0NTY3ODk6Q0RFRkdISUpTVFVWV1hZWmNkZWZnaGlqc3R1dnd"
    "4eXqDhIWGh4iJipKTlJWWl5iZmqKjpKWmp6ipqrKztLW2t7i5usLDxMXGx8jJytLT1NXW1"
    "9jZ2uHi4+Tl5ufo6erx8vP09fb3+Pn6/8QAHwEAAwEBAQEBAQEBAQAAAAAAAAECAwQFBgc"
    "ICQoL/8QAtREAAgECBAQDBAcFBAQAAQJ3AAECAxEEBSExBhJBUQdhcRMiMoEIFEKRobHBC"
    "SMzUvAVYnLRChYkNOEl8RcYGRomJygpKjU2Nzg5OkNERUZHSElKU1RVVldYWVpjZGVmZ2h"
    "panN0dXZ3eHl6goOEhYaHiImKkpOUlZaXmJmaoqOkpaanqKmqsrO0tba3uLm6wsPExcbHy"
    "MnK0tPU1dbX2Nna4uPk5ebn6Onq8vP09fb3+Pn6/9oADAMBAAIRAxEAPwD2aiiigD//2Q=="
)


def _scw_configured() -> bool:
    """True iff Scaleway API credentials + at least one GPU are set."""
    return bool(settings.scw_access_key and settings.scw_secret_key and settings.gpu_candidates)


def _backend_ready(backend_url: str) -> bool:
    """Quick liveness check — vLLM has the model loaded."""
    url = backend_url.rstrip("/")
    try:
        r = httpx.get(f"{url}/v1/models", timeout=3.0)
        return r.status_code == 200
    except httpx.HTTPError:
        return False


def _backend_serves_inference(backend_url: str) -> bool:
    """Real readiness probe — send a tiny image and require a 200 back.

    Needed because vLLM's ``/v1/models`` starts answering 200 as soon as the
    weights are loaded, but the first inference request can still 500 while
    CUDA graphs are being compiled.
    """
    url = backend_url.rstrip("/")
    try:
        with httpx.Client(timeout=30.0) as client:
            r = client.post(
                f"{url}/v1/chat/completions",
                json={
                    "model": settings.backend_model,
                    "messages": [
                        {
                            "role": "user",
                            "content": [
                                {
                                    "type": "image_url",
                                    "image_url": {
                                        "url": f"data:image/jpeg;base64,{_SMOKE_JPEG_B64}"
                                    },
                                },
                                {"type": "text", "text": "ok"},
                            ],
                        }
                    ],
                    "max_tokens": 1,
                    "temperature": 0.0,
                },
            )
            return r.status_code == 200
    except httpx.HTTPError:
        return False


def _scw_action(action: str, server_id: str, zone: str) -> tuple[int, str]:
    """POST a power action to Scaleway. Returns ``(status_code, body)``.

    Caller is responsible for interpreting the result. Network failures
    return ``(0, str(exc))``.
    """
    url = f"{_SCW_API_BASE}/zones/{zone}/servers/{server_id}/action"
    headers = {
        "X-Auth-Token": settings.scw_secret_key,
        "Content-Type": "application/json",
    }
    try:
        r = httpx.post(url, json={"action": action}, headers=headers, timeout=15)
        return r.status_code, r.text[:500]
    except httpx.HTTPError as e:
        return 0, str(e)


def _scw_poweron(server_id: str, zone: str) -> PowerOnResult:
    """Request Scaleway poweron. Differentiates out_of_stock from other errors.

    - 200/2xx → ``"ok"`` (or server was already running)
    - 412 with ``"out_of_stock"`` in body → ``"out_of_stock"``
    - anything else → ``"error"``
    """
    status, body = _scw_action("poweron", server_id, zone)
    if 200 <= status < 300:
        return "ok"
    if status == 412 and "out_of_stock" in body:
        logger.warning("Scaleway poweron %s: out_of_stock", server_id[:8])
        return "out_of_stock"
    logger.warning("Scaleway poweron %s returned %s: %s", server_id[:8], status, body[:200])
    return "error"


def _scw_poweroff(server_id: str, zone: str) -> None:
    """Idempotent power-off. Logs warnings but never raises."""
    status, body = _scw_action("poweroff", server_id, zone)
    if status >= 400:
        logger.warning("Scaleway poweroff %s returned %s: %s", server_id[:8], status, body[:200])


# Server states that indicate the box is *not* coming up. Scaleway sometimes
# accepts a poweron with 200 ok and then silently flips the box back to
# ``archived`` a few seconds later (capacity reshuffle, internal aborts).
# Catching that early lets us fall through to the next candidate instead of
# waiting the full boot-timeout window for vLLM that will never appear.
_NOT_RUNNING_STATES = frozenset({"archived", "stopped", "stopped in place", "locked"})


def _scw_get_state(server_id: str, zone: str) -> str:
    """Fetch current state of a Scaleway instance. Returns ``"unknown"`` on error."""
    url = f"{_SCW_API_BASE}/zones/{zone}/servers/{server_id}"
    headers = {"X-Auth-Token": settings.scw_secret_key}
    try:
        r = httpx.get(url, headers=headers, timeout=5)
        if r.status_code == 200:
            return r.json().get("server", {}).get("state", "unknown")
        logger.warning("Scaleway state fetch %s returned %s", server_id[:8], r.status_code)
        return "unknown"
    except httpx.HTTPError as e:
        logger.warning("Scaleway state fetch %s failed: %s", server_id[:8], e)
        return "unknown"


def _verify_poweron_holds(server_id: str, zone: str, hold_seconds: int = 30) -> bool:
    """Poll instance state for ``hold_seconds`` after a successful poweron.

    Returns ``True`` iff the instance stays in a non-stopped state for the full
    window. ``False`` as soon as it flips back to archived/stopped/locked —
    that's our signal that Scaleway pulled the box silently and we should try
    the fallback immediately instead of waiting for vLLM to come up.
    """
    deadline = time.time() + hold_seconds
    while time.time() < deadline:
        state = _scw_get_state(server_id, zone)
        if state in _NOT_RUNNING_STATES:
            logger.warning(
                "Server %s reverted to '%s' after poweron — not a real boot",
                server_id[:8],
                state,
            )
            return False
        time.sleep(2)
    return True


def ensure_any_gpu_running() -> str:
    """Block until *some* configured GPU can serve inference.

    Tries candidates in order (primary first). For each candidate:

    1. If it already answers /v1/models=200 and passes the inference
       smoke-test, return its URL immediately.
    2. Otherwise request a Scaleway poweron. If Scaleway reports
       ``out_of_stock``, skip to the next candidate without waiting.
    3. Poll readiness for up to ``_BOOT_TIMEOUT_SECONDS``. On success
       return the URL; on timeout move to the next candidate.

    Returns the backend URL of the GPU that is ready. Raises
    ``RuntimeError`` if no candidate becomes ready.
    """
    candidates = settings.gpu_candidates
    if not candidates:
        # No Scaleway config — assume the operator runs the backend themselves.
        # Caller (worker) falls back to settings.backend_url.
        logger.warning("No GPU candidates configured — returning primary URL as-is")
        return settings.backend_url

    # Fast path: any candidate already serving?
    for label, _server_id, _zone, backend_url in candidates:
        if _backend_ready(backend_url) and _backend_serves_inference(backend_url):
            logger.info("GPU %s already ready at %s", label, backend_url)
            return backend_url

    if not (settings.scw_access_key and settings.scw_secret_key):
        logger.warning("Scaleway credentials missing — will wait only on primary")
        # Fall through to the polling loop on the first candidate
        return _wait_for_backend(candidates[0][3])

    last_error: str | None = None
    for label, server_id, zone, backend_url in candidates:
        logger.info("Trying GPU %s (%s)", label, server_id[:8])
        outcome = _scw_poweron(server_id, zone)
        if outcome == "out_of_stock":
            logger.info("GPU %s out of stock — trying next candidate", label)
            last_error = f"{label} out_of_stock"
            continue
        if outcome == "error":
            logger.warning("GPU %s poweron errored — trying next candidate", label)
            last_error = f"{label} poweron error"
            continue
        # "ok" — but verify the box doesn't get pulled back by Scaleway before
        # committing to the 10-min boot wait.
        if not _verify_poweron_holds(server_id, zone):
            last_error = f"{label} poweron reverted"
            continue
        try:
            return _wait_for_backend(backend_url)
        except RuntimeError as e:
            logger.warning("GPU %s did not become ready: %s", label, e)
            last_error = f"{label} boot timeout"
            continue

    raise RuntimeError(f"No GPU became ready. Last: {last_error or 'unknown'}")


def _wait_for_backend(backend_url: str) -> str:
    """Poll until ``backend_url`` passes the inference smoke-test, or time out.

    Returns ``backend_url`` on success. Raises ``RuntimeError`` on timeout.
    """
    deadline = time.time() + _BOOT_TIMEOUT_SECONDS
    logged_serving_wait = False
    while time.time() < deadline:
        if _backend_ready(backend_url):
            if not logged_serving_wait:
                logger.info("Backend %s live (/v1/models 200) — waiting for inference", backend_url)
                logged_serving_wait = True
            if _backend_serves_inference(backend_url):
                logger.info("Backend %s ready (smoke test passed)", backend_url)
                return backend_url
        time.sleep(5)
    raise RuntimeError(
        f"Backend {backend_url} not ready after {_BOOT_TIMEOUT_SECONDS}s boot window"
    )


def ensure_gpu_running() -> None:
    """Legacy entry point — kept for existing call sites.

    Delegates to :func:`ensure_any_gpu_running` and discards the return value.
    New callers should use ``ensure_any_gpu_running()`` directly and pass the
    URL to the pipeline.
    """
    ensure_any_gpu_running()


async def shutdown_gpu_if_idle(redis_pool) -> None:
    """Stop all configured GPUs via Scaleway API if no jobs are queued.

    Iterates over all GPU candidates (primary + fallback) and issues poweroff
    to each. Scaleway's poweroff is idempotent on already-archived servers.
    """
    if not _scw_configured():
        return

    try:
        if redis_pool is not None:
            queued = await redis_pool.zcard("arq:queue")
            if queued and queued > 0:
                logger.info("GPU kept running: %d jobs still queued", queued)
                return
    except Exception as e:
        logger.warning("Queue check failed (%s) — keeping GPU up as precaution", e)
        return

    logger.info("No pending jobs — stopping all configured GPUs")
    for label, server_id, zone, _ in settings.gpu_candidates:
        logger.info("Power off %s (%s)", label, server_id[:8])
        _scw_poweroff(server_id, zone)
