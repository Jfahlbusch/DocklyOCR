"""On-demand GPU management via Scaleway API.

Called by the ARQ worker before each OCR job. If the GPU instance is
powered off, this module boots it and waits until vLLM is responsive.
Already-running GPUs are detected via a quick health ping — no Scaleway
API call needed.

Requires these settings (optional — only used if all are set):

    scw_access_key      — Scaleway API access key
    scw_secret_key      — Scaleway API secret key
    scw_gpu_server_id   — Instance UUID of the GPU server
    scw_gpu_zone        — Zone (e.g. fr-par-2)

If any are empty, this module is a no-op (assumes GPU is always on).
"""

from __future__ import annotations

import logging
import time

import httpx

from app.config import settings

logger = logging.getLogger(__name__)

_SCW_API_BASE = "https://api.scaleway.com/instance/v1"
_BOOT_TIMEOUT_SECONDS = 600  # 10 min: GPU boot (20s) + vLLM load + CUDA graph compile

# 8x8 white JPEG — smallest payload that exercises the full vision pipeline.
# Used by _backend_serves_inference() as a real warmup smoke-test so we don't
# return from ensure_gpu_running() while vLLM still reports /v1/models=200
# but answers 500 to real image requests (CUDA graph compile in progress).
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
    return bool(
        settings.scw_access_key
        and settings.scw_secret_key
        and settings.scw_gpu_server_id
        and settings.scw_gpu_zone
    )


def _backend_ready() -> bool:
    """Quick liveness check — vLLM has the model loaded.

    Returns True as soon as ``/v1/models`` answers 200. This is *not* a
    guarantee that inference works (CUDA graphs may still be compiling);
    use :func:`_backend_serves_inference` for that.
    """
    url = settings.backend_url.rstrip("/")
    try:
        r = httpx.get(f"{url}/v1/models", timeout=3.0)
        return r.status_code == 200
    except httpx.HTTPError:
        return False


def _backend_serves_inference() -> bool:
    """Real readiness probe — send a tiny image and require a 200 back.

    Needed because vLLM's ``/v1/models`` starts answering 200 as soon as the
    weights are loaded, but the first inference request can still 500 while
    CUDA graphs are being compiled. Firing the pipeline's 12 parallel
    requests during that window ends with ``pages_ok == 0`` for the whole
    document.
    """
    url = settings.backend_url.rstrip("/")
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


def _scw_poweron() -> None:
    """Send power-on action to Scaleway. Safe if already running."""
    url = (
        f"{_SCW_API_BASE}/zones/{settings.scw_gpu_zone}/servers/{settings.scw_gpu_server_id}/action"
    )
    headers = {
        "X-Auth-Token": settings.scw_secret_key,
        "Content-Type": "application/json",
    }
    try:
        r = httpx.post(url, json={"action": "poweron"}, headers=headers, timeout=15)
        if r.status_code >= 400:
            logger.warning("Scaleway poweron returned %s: %s", r.status_code, r.text[:200])
    except httpx.HTTPError as e:
        logger.warning("Scaleway poweron call failed: %s", e)


def ensure_gpu_running() -> None:
    """Block until the GPU backend can actually serve inference.

    Two-stage readiness:

    1. ``/v1/models`` must answer 200 (liveness).
    2. A real image-inference request must succeed (serving).

    Stage 2 is what separates "model loaded" from "CUDA graphs compiled,
    ready to serve". Without it the pipeline's 12 parallel requests can
    land during warmup and all 500, yielding ``pages_ok == 0``.

    Boots the GPU via Scaleway API when needed. No-op when Scaleway creds
    aren't configured (caller is expected to start the backend).
    """
    if _backend_ready() and _backend_serves_inference():
        return

    if _scw_configured():
        logger.info("Backend not ready — requesting Scaleway poweron")
        _scw_poweron()
    else:
        logger.warning("Backend not ready and Scaleway credentials missing — will wait only")

    deadline = time.time() + _BOOT_TIMEOUT_SECONDS
    logged_serving_wait = False
    while time.time() < deadline:
        if _backend_ready():
            if not logged_serving_wait:
                logger.info("Backend live (/v1/models 200) — waiting for inference readiness")
                logged_serving_wait = True
            if _backend_serves_inference():
                logger.info("Backend ready after boot (smoke test passed)")
                return
        time.sleep(5)

    raise RuntimeError(f"OCR backend still not ready after {_BOOT_TIMEOUT_SECONDS}s boot window")


def _scw_poweroff() -> None:
    """Send power-off action to Scaleway. Idempotent."""
    url = (
        f"{_SCW_API_BASE}/zones/{settings.scw_gpu_zone}/servers/{settings.scw_gpu_server_id}/action"
    )
    headers = {
        "X-Auth-Token": settings.scw_secret_key,
        "Content-Type": "application/json",
    }
    try:
        r = httpx.post(url, json={"action": "poweroff"}, headers=headers, timeout=15)
        if r.status_code >= 400:
            logger.warning("Scaleway poweroff returned %s: %s", r.status_code, r.text[:200])
    except httpx.HTTPError as e:
        logger.warning("Scaleway poweroff call failed: %s", e)


async def shutdown_gpu_if_idle(redis_pool) -> None:
    """Stop the GPU via Scaleway API if no jobs are queued or in-flight.

    Called by the worker after each job completes. Gives immediate shutdown
    without waiting for the 5-min GPU-side safety timer. If the call fails,
    the GPU-side timer is the fallback.

    ``redis_pool`` is the ARQ context's redis handle (``ctx["redis"]``).
    """
    if not _scw_configured():
        return

    # ARQ stores queued jobs in a sorted set at "arq:queue"
    try:
        if redis_pool is not None:
            queued = await redis_pool.zcard("arq:queue")
            if queued and queued > 0:
                logger.info("GPU kept running: %d jobs still queued", queued)
                return
    except Exception as e:
        logger.warning("Queue check failed (%s) — keeping GPU up as precaution", e)
        return

    logger.info("No pending jobs — stopping GPU via Scaleway API")
    _scw_poweroff()
