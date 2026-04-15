"""Tests for `app/routers/ocr.py` — POST /v1/ocr endpoint.

These tests build a minimal FastAPI app that mounts only the OCR router and
the content-length middleware, then overrides the ``get_session`` dependency
with a test SQLite engine and monkey-patches the OCR pipeline + storage.

No real Redis, Ollama, or network calls are made.
"""

from __future__ import annotations

import io
from collections.abc import Iterator
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy.pool import StaticPool
from sqlmodel import Session, SQLModel, create_engine

from app.auth import generate_api_key
from app.db import get_session
from app.middleware import ContentLengthLimitMiddleware
from app.models import ApiKey, Customer, Job, JobStatus
from app.routers import ocr as ocr_router_module
from app.services import storage as storage_module
from app.services.ocr_pipeline import OcrResult, PageResult

# --- Fixtures --------------------------------------------------------------


@pytest.fixture()
def db_engine():
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    SQLModel.metadata.create_all(engine)
    yield engine
    SQLModel.metadata.drop_all(engine)


@pytest.fixture()
def session(db_engine) -> Iterator[Session]:
    with Session(db_engine) as s:
        yield s


@pytest.fixture(autouse=True)
def _patch_storage(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Redirect the module-level ``storage`` to a tmp directory."""
    from app.services.storage import LocalStorage

    new_storage = LocalStorage(tmp_path / "test_storage")
    monkeypatch.setattr(storage_module, "storage", new_storage)
    monkeypatch.setattr(ocr_router_module, "storage", new_storage)


@pytest.fixture(autouse=True)
def _patch_run_ocr(monkeypatch: pytest.MonkeyPatch) -> None:
    """Patch ``run_ocr`` to return a canned result — no real pipeline."""

    def fake_run_ocr(input_path: Path, tmp_dir: Path):
        return OcrResult(
            pages=[
                PageResult(
                    number=1,
                    text="Hello world",
                    strategy="150dpi/1024px",
                    elapsed_s=0.01,
                )
            ],
            page_count=1,
            pages_ok=1,
            pages_failed=0,
        )

    monkeypatch.setattr(ocr_router_module, "run_ocr", fake_run_ocr)


@pytest.fixture()
def app(db_engine) -> FastAPI:
    fastapi_app = FastAPI()
    # Attach a stub ARQ pool with an AsyncMock .enqueue_job by default.
    fastapi_app.state.arq_pool = SimpleNamespace(enqueue_job=AsyncMock(return_value=None))

    fastapi_app.add_middleware(ContentLengthLimitMiddleware, max_bytes=1024 * 1024)

    # Inject shared test session for the OCR router
    def _override_session() -> Iterator[Session]:
        with Session(db_engine) as s:
            yield s

    fastapi_app.dependency_overrides[get_session] = _override_session
    fastapi_app.include_router(ocr_router_module.router, prefix="/v1")
    return fastapi_app


@pytest.fixture()
def client(app: FastAPI) -> TestClient:
    return TestClient(app)


@pytest.fixture()
def api_key(session: Session) -> str:
    """Seed a customer + key, return the plaintext."""
    from app.auth import hash_api_key

    customer = Customer(name="Acme", email="acme-ocr@example.com")
    session.add(customer)
    session.commit()
    session.refresh(customer)

    plaintext, key_hash, prefix = generate_api_key()
    key = ApiKey(
        customer_id=customer.id,  # type: ignore[arg-type]
        key_hash=key_hash,
        key_prefix=prefix,
        name="test",
    )
    session.add(key)
    session.commit()
    # Silence unused import warning
    _ = hash_api_key
    return plaintext


# --- Tests -----------------------------------------------------------------


def test_sync_ocr_valid_pdf_returns_body(
    client: TestClient, api_key: str, session: Session
) -> None:
    fake_pdf = b"%PDF-1.4\n%EOF\n"
    resp = client.post(
        "/v1/ocr",
        headers={"X-API-Key": api_key},
        files={"file": ("sample.pdf", io.BytesIO(fake_pdf), "application/pdf")},
        data={"output_format": "md", "mode": "sync"},
    )
    assert resp.status_code == 200, resp.text
    assert resp.headers["content-type"].startswith("text/markdown")
    assert "Hello world" in resp.text
    assert "attachment" in resp.headers["content-disposition"]
    assert 'filename="sample.md"' in resp.headers["content-disposition"]

    # Verify the Job row was updated to "done"
    jobs = session.exec(__import__("sqlmodel").select(Job)).all()
    assert len(jobs) == 1
    assert jobs[0].status == JobStatus.done
    assert jobs[0].page_count == 1


def test_sync_txt_format(client: TestClient, api_key: str) -> None:
    resp = client.post(
        "/v1/ocr",
        headers={"X-API-Key": api_key},
        files={"file": ("doc.pdf", io.BytesIO(b"%PDF-1.4\n"), "application/pdf")},
        data={"output_format": "txt", "mode": "sync"},
    )
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("text/plain")
    assert 'filename="doc.txt"' in resp.headers["content-disposition"]


def test_sync_json_format(client: TestClient, api_key: str) -> None:
    resp = client.post(
        "/v1/ocr",
        headers={"X-API-Key": api_key},
        files={"file": ("report.pdf", io.BytesIO(b"%PDF-1.4\n"), "application/pdf")},
        data={"output_format": "json", "mode": "sync"},
    )
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("application/json")
    import json as _json

    payload = _json.loads(resp.text)
    assert "meta" in payload
    assert payload["meta"]["page_count"] == 1


def test_invalid_mime_returns_415(client: TestClient, api_key: str) -> None:
    resp = client.post(
        "/v1/ocr",
        headers={"X-API-Key": api_key},
        files={"file": ("note.txt", io.BytesIO(b"hello"), "text/plain")},
        data={"output_format": "md", "mode": "sync"},
    )
    assert resp.status_code == 415
    assert "Unsupported media type" in resp.json()["detail"]


def test_invalid_output_format_returns_400(client: TestClient, api_key: str) -> None:
    resp = client.post(
        "/v1/ocr",
        headers={"X-API-Key": api_key},
        files={"file": ("doc.pdf", io.BytesIO(b"%PDF-1.4\n"), "application/pdf")},
        data={"output_format": "yaml", "mode": "sync"},
    )
    assert resp.status_code == 400
    assert "Invalid output_format" in resp.json()["detail"]


def test_missing_api_key_returns_401_or_422(client: TestClient) -> None:
    resp = client.post(
        "/v1/ocr",
        files={"file": ("doc.pdf", io.BytesIO(b"%PDF-1.4\n"), "application/pdf")},
        data={"output_format": "md", "mode": "sync"},
    )
    # FastAPI Header(...) returns 422 for missing required header
    assert resp.status_code in (401, 422)


def test_wrong_api_key_returns_401(client: TestClient) -> None:
    resp = client.post(
        "/v1/ocr",
        headers={"X-API-Key": "sk_live_not_a_real_key"},
        files={"file": ("doc.pdf", io.BytesIO(b"%PDF-1.4\n"), "application/pdf")},
        data={"output_format": "md", "mode": "sync"},
    )
    assert resp.status_code == 401


def test_oversized_upload_returns_413(
    db_engine, api_key: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Build a tiny-limit app and send a body larger than the limit."""
    # Fresh app with a 512-byte limit
    app = FastAPI()
    app.state.arq_pool = SimpleNamespace(enqueue_job=AsyncMock())
    app.add_middleware(ContentLengthLimitMiddleware, max_bytes=512)

    def _override_session() -> Iterator[Session]:
        with Session(db_engine) as s:
            yield s

    app.dependency_overrides[get_session] = _override_session
    app.include_router(ocr_router_module.router, prefix="/v1")

    client = TestClient(app)
    big_body = b"%PDF-1.4\n" + b"A" * 1024

    resp = client.post(
        "/v1/ocr",
        headers={"X-API-Key": api_key},
        files={"file": ("big.pdf", io.BytesIO(big_body), "application/pdf")},
        data={"output_format": "md", "mode": "sync"},
    )
    assert resp.status_code == 413
    assert resp.json()["error"] == "Payload too large"
    assert resp.json()["max_bytes"] == 512


def test_async_mode_enqueues_and_returns_202(
    client: TestClient, app: FastAPI, api_key: str
) -> None:
    resp = client.post(
        "/v1/ocr",
        headers={"X-API-Key": api_key},
        files={"file": ("doc.pdf", io.BytesIO(b"%PDF-1.4\n"), "application/pdf")},
        data={"output_format": "md", "mode": "async"},
    )
    assert resp.status_code == 202, resp.text
    payload = resp.json()
    assert "job_id" in payload
    assert payload["status"] == "pending"
    assert payload["status_url"] == f"/v1/jobs/{payload['job_id']}"

    # The stubbed ARQ pool must have been called exactly once
    app.state.arq_pool.enqueue_job.assert_called_once()
    call = app.state.arq_pool.enqueue_job.call_args
    assert call.args[0] == "process_ocr_job"
    assert call.args[1] == payload["job_id"]


def test_async_mode_without_pool_returns_503(db_engine, api_key: str) -> None:
    """If the app has no ARQ pool on state, async submission returns 503."""
    app = FastAPI()
    # Explicitly no arq_pool on state
    app.add_middleware(ContentLengthLimitMiddleware, max_bytes=10 * 1024 * 1024)

    def _override_session() -> Iterator[Session]:
        with Session(db_engine) as s:
            yield s

    app.dependency_overrides[get_session] = _override_session
    app.include_router(ocr_router_module.router, prefix="/v1")
    client = TestClient(app)

    resp = client.post(
        "/v1/ocr",
        headers={"X-API-Key": api_key},
        files={"file": ("doc.pdf", io.BytesIO(b"%PDF-1.4\n"), "application/pdf")},
        data={"output_format": "md", "mode": "async"},
    )
    assert resp.status_code == 503


def test_async_mode_webhook_url_persisted(
    client: TestClient, api_key: str, session: Session
) -> None:
    resp = client.post(
        "/v1/ocr",
        headers={"X-API-Key": api_key},
        files={"file": ("doc.pdf", io.BytesIO(b"%PDF-1.4\n"), "application/pdf")},
        data={
            "output_format": "json",
            "mode": "async",
            "webhook_url": "https://my-app.com/hook",
        },
    )
    assert resp.status_code == 202
    job_id = resp.json()["job_id"]

    session.expire_all()
    job = session.get(Job, job_id)
    assert job is not None
    assert job.webhook_url == "https://my-app.com/hook"
    assert job.status == JobStatus.pending
