from contextlib import asynccontextmanager, suppress

import httpx
from arq.connections import RedisSettings, create_pool
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse
from starlette.middleware.sessions import SessionMiddleware

from app.config import settings
from app.middleware import ContentLengthLimitMiddleware
from app.routers import admin, jobs, ocr
from app.schemas import HealthResponse

OPENAPI_TAGS = [
    {
        "name": "ocr",
        "description": (
            "Submit PDFs or images for text extraction. Supports sync (wait for "
            "result) and async (webhook/poll) modes. All endpoints require "
            "`X-API-Key` authentication."
        ),
    },
    {
        "name": "jobs",
        "description": (
            "Inspect job status, fetch result files, and list past jobs. Scoped "
            "to the caller's API key. All endpoints require `X-API-Key` "
            "authentication."
        ),
    },
    {
        "name": "health",
        "description": (
            "Operational health probe. Public (no auth), intended for load "
            "balancers and uptime monitors."
        ),
    },
]


@asynccontextmanager
async def lifespan(app: FastAPI):
    try:
        app.state.arq_pool = await create_pool(RedisSettings.from_dsn(settings.redis_url))
    except Exception:
        app.state.arq_pool = None
    try:
        yield
    finally:
        pool = getattr(app.state, "arq_pool", None)
        if pool is not None:
            with suppress(Exception):
                await pool.aclose()


app = FastAPI(
    title="DocklyOCR",
    description=(
        "Self-hosted OCR API powered by Qwen2.5-VL via vLLM, with a "
        "multi-strategy fallback pipeline (column-split detection, "
        "re-scans, table re-prompt). Accepts PDFs and images, returns "
        "Markdown, plain text, TOON, or JSON — synchronously, async via "
        "webhook, or batched."
    ),
    version="0.1.0",
    openapi_tags=OPENAPI_TAGS,
    lifespan=lifespan,
)


if settings.allowed_origins_list:
    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.allowed_origins_list,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )


app.add_middleware(
    SessionMiddleware,
    secret_key=settings.session_secret,
    session_cookie="dockly_admin",
    max_age=60 * 60 * 8,
    same_site="lax",
    https_only=False,
)

app.add_middleware(
    ContentLengthLimitMiddleware,
    max_bytes=settings.max_upload_bytes,
)


app.include_router(ocr.router, prefix="/v1")
app.include_router(jobs.router, prefix="/v1")
app.include_router(admin.router)


# Scalar API reference — modern UI that actually supports multi-file upload
# (Swagger UI's "Try it out" fails on array<file>; ReDoc is read-only).
@app.get("/scalar", include_in_schema=False, response_class=HTMLResponse)
async def scalar_docs() -> HTMLResponse:
    return HTMLResponse(
        """<!doctype html>
<html>
  <head>
    <title>DocklyOCR — API Reference</title>
    <meta charset="utf-8" />
    <meta name="viewport" content="width=device-width, initial-scale=1" />
  </head>
  <body>
    <script id="api-reference" data-url="/openapi.json"></script>
    <script src="https://cdn.jsdelivr.net/npm/@scalar/api-reference"></script>
  </body>
</html>"""
    )


async def _check_backend() -> str:
    """Ping the vLLM backend. Returns a short status."""
    try:
        url = settings.backend_url.rstrip("/")
        async with httpx.AsyncClient(timeout=2.0) as client:
            r = await client.get(f"{url}/v1/models")
            if r.status_code == 200:
                return "ok"
            return f"status_{r.status_code}"
    except httpx.HTTPError:
        return "unreachable"


async def _check_db() -> str:
    try:
        from sqlmodel import Session, text

        from app.db import engine

        with Session(engine) as session:
            session.exec(text("SELECT 1"))
        return "ok"
    except Exception:
        return "unreachable"


@app.get(
    "/health",
    tags=["health"],
    summary="Service health probe",
    description=(
        "Returns the combined readiness status of the API, its database, and "
        "the vLLM OCR backend. Public endpoint: no authentication required. "
        "Intended for load balancers, uptime monitors, and Kubernetes "
        "readiness probes."
    ),
    response_model=HealthResponse,
)
async def health():
    backend_status = await _check_backend()
    db_status = await _check_db()
    overall = "ok" if backend_status == "ok" and db_status == "ok" else "degraded"
    return {
        "status": overall,
        "backend": backend_status,
        "db": db_status,
    }
