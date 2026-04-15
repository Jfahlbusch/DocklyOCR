from contextlib import asynccontextmanager

import httpx
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from starlette.middleware.sessions import SessionMiddleware

from app.config import settings

OPENAPI_TAGS = [
    {"name": "ocr", "description": "Submit files for OCR processing (sync or async)."},
    {"name": "jobs", "description": "Inspect job status and download results."},
    {"name": "admin", "description": "Admin UI for managing customers and API keys."},
    {"name": "health", "description": "Service health and readiness probes."},
]


@asynccontextmanager
async def lifespan(app: FastAPI):
    yield


app = FastAPI(
    title="DocklyOCR",
    description=(
        "Self-hosted OCR API based on glm-ocr (Ollama) with a 13-strategy "
        "multi-fallback pipeline. Accepts PDFs and images, returns Markdown, "
        "plain text, TOON, or JSON synchronously or via webhook."
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


async def _check_ollama() -> str:
    try:
        async with httpx.AsyncClient(timeout=2.0) as client:
            r = await client.get(f"{settings.ollama_url.rstrip('/')}/api/tags")
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


@app.get("/health", tags=["health"], summary="Service health probe")
async def health():
    ollama_status = await _check_ollama()
    db_status = await _check_db()
    overall = "ok" if ollama_status == "ok" and db_status == "ok" else "degraded"
    return {
        "status": overall,
        "ollama": ollama_status,
        "db": db_status,
    }
