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
_BOOT_TIMEOUT_SECONDS = 300  # 5 minutes from poweron to vLLM ready


def _scw_configured() -> bool:
    return bool(
        settings.scw_access_key
        and settings.scw_secret_key
        and settings.scw_gpu_server_id
        and settings.scw_gpu_zone
    )


def _backend_ready() -> bool:
    """Quick check: is the OCR backend (vLLM or Ollama) responsive?"""
    url = settings.ollama_url.rstrip("/")
    path = "/v1/models" if settings.ollama_use_openai_api else "/api/tags"
    try:
        r = httpx.get(f"{url}{path}", timeout=3.0)
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
    """Block until the GPU backend is ready. Boot it via Scaleway API if needed.

    Flow:
    1. Quick health check (3s) — if backend responds, return immediately
    2. If not, power on via Scaleway API
    3. Poll /v1/models (or /api/tags) every 5s until ready, max 5 min
    4. Raise RuntimeError if still not ready after timeout

    If Scaleway credentials aren't configured, skip the boot step and just
    wait for the backend to come up (someone else is expected to start it).
    """
    if _backend_ready():
        return

    if _scw_configured():
        logger.info("Backend not ready — requesting Scaleway poweron")
        _scw_poweron()
    else:
        logger.warning("Backend not ready and Scaleway credentials missing — will wait only")

    deadline = time.time() + _BOOT_TIMEOUT_SECONDS
    while time.time() < deadline:
        if _backend_ready():
            logger.info("Backend ready after boot")
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
