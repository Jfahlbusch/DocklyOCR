"""End-to-end integration tests for DocklyOCR.

These tests wire the real ``app.main.app`` FastAPI instance against an
in-memory SQLite engine, a temp storage dir, and a mocked backend call —
then exercise the full request path: HTTP -> router -> auth -> pipeline
-> formatter -> storage -> (for async) worker -> webhook.

The only external calls stubbed out are:

* ``_call_backend`` in ``app.services.ocr_pipeline`` — replaced with a
  function returning a canned OCR string so the 13-strategy pipeline
  picks the first strategy and succeeds immediately.
* ``subprocess.run`` inside ``app.workers.ocr_worker`` — replaced with
  an in-process shim that invokes the real ``run_ocr`` pipeline and
  serializes the ``OcrResult`` to the JSON file the worker expects. This
  is done because the real subprocess would not inherit the
  ``_call_backend`` monkey-patch.
* ``httpx.AsyncClient`` factory inside ``app.services.webhook`` — replaced
  with a ``httpx.MockTransport`` that captures the outbound POST.

Tests requiring PDF rasterisation are auto-skipped when ``pdftoppm`` is not
available on ``$PATH`` (documented on each skipped test).
"""

from __future__ import annotations

import json
import shutil
import subprocess
from collections.abc import Iterator
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest
from fastapi.testclient import TestClient
from sqlalchemy.pool import StaticPool
from sqlmodel import Session, SQLModel, create_engine, select

from app.auth import generate_api_key, rate_limiter
from app.db import get_session
from app.main import app as real_app
from app.models import ApiKey, Customer, Job, JobStatus
from app.services import ocr_pipeline as ocr_pipeline_module
from app.services import storage as storage_module
from app.services import webhook as webhook_module
from app.services.ocr_pipeline import run_ocr
from app.services.storage import LocalStorage
from app.workers import ocr_worker as ocr_worker_module

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

FIXTURES_DIR = Path(__file__).parent / "fixtures"
SAMPLE_PDF = FIXTURES_DIR / "sample.pdf"
FAKE_OCR_TEXT = "FAKE OCR PAGE TEXT"

needs_pdftoppm = pytest.mark.skipif(
    shutil.which("pdftoppm") is None or shutil.which("pdfinfo") is None,
    reason="pdftoppm/pdfinfo not available — PDF rasterisation cannot run",
)


# ---------------------------------------------------------------------------
# Engine / session / storage / backend mock
# ---------------------------------------------------------------------------


@pytest.fixture()
def db_engine():
    """Fresh in-memory SQLite per test with all tables created."""
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


@pytest.fixture()
def tmp_storage(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> LocalStorage:
    """Redirect every import of ``storage`` to a per-test temp directory."""
    new_storage = LocalStorage(tmp_path / "storage")

    # Replace the module attribute *and* every router module that imported
    # the name at import-time (`from ... import storage`). If we miss one,
    # that module keeps its bound reference to the production singleton
    # pointing at /data/storage, which isn't writable on the test host.
    from app.routers import jobs as jobs_router_module
    from app.routers import ocr as ocr_router_module

    monkeypatch.setattr(storage_module, "storage", new_storage)
    monkeypatch.setattr(ocr_router_module, "storage", new_storage)
    monkeypatch.setattr(jobs_router_module, "storage", new_storage)
    monkeypatch.setattr(ocr_worker_module, "storage", new_storage)
    return new_storage


@pytest.fixture()
def mocked_backend(monkeypatch: pytest.MonkeyPatch) -> None:
    """Patch ``_call_backend`` so the pipeline runs end-to-end without calling the OCR backend.

    Because ``try_ocr`` resolves ``_call_backend`` by module-level name lookup
    at call time, patching ``app.services.ocr_pipeline._call_backend`` is
    enough — no need to reach into ``try_ocr`` itself.
    """

    def _fake_call_backend(img_path: Path) -> str:  # noqa: ARG001
        return FAKE_OCR_TEXT

    monkeypatch.setattr(ocr_pipeline_module, "_call_backend", _fake_call_backend)


@pytest.fixture()
def reset_rate_limiter() -> Iterator[None]:
    """Reset the singleton in-memory rate limiter between tests.

    ``app.main.app`` uses the module-level ``rate_limiter`` whose state leaks
    across tests (each request still counts against the sliding window). We
    reset both before and after so each test starts with a clean bucket.
    """
    rate_limiter.reset()
    yield
    rate_limiter.reset()


# ---------------------------------------------------------------------------
# Test FastAPI client — real app, swapped session + stub arq pool
# ---------------------------------------------------------------------------


@pytest.fixture()
def client(
    db_engine,
    tmp_storage: LocalStorage,  # noqa: ARG001 -- ensure storage is patched
    mocked_backend,  # noqa: ARG001 -- ensure backend is mocked before requests
    reset_rate_limiter,  # noqa: ARG001 -- ensure clean rate-limit state
    monkeypatch: pytest.MonkeyPatch,
) -> Iterator[TestClient]:
    """TestClient against the real ``app.main.app`` with test overrides.

    The real ``lifespan`` hook tries to connect to Redis — which isn't
    available in tests — and retries for several seconds before assigning
    ``app.state.arq_pool = None``. We short-circuit ``create_pool`` with an
    AsyncMock so the lifespan startup is instant, then install our stub
    pool **after** the TestClient has entered its context (so the
    ``lifespan``'s fallback assignment doesn't overwrite it).
    """

    def _override_session() -> Iterator[Session]:
        with Session(db_engine) as s:
            yield s

    previous_overrides = dict(real_app.dependency_overrides)
    real_app.dependency_overrides[get_session] = _override_session

    # Prevent lifespan startup from blocking on Redis retries.
    import app.main as main_module

    async def _fake_create_pool(*args, **kwargs):  # noqa: ARG001
        return SimpleNamespace(
            enqueue_job=AsyncMock(return_value=None),
            aclose=AsyncMock(return_value=None),
        )

    monkeypatch.setattr(main_module, "create_pool", _fake_create_pool)

    with TestClient(real_app) as tc:
        # Re-install our stub on ``state`` in case lifespan overwrote it
        # (it doesn't, now that create_pool is mocked — but belt & braces).
        previous_arq_pool = getattr(real_app.state, "arq_pool", None)
        real_app.state.arq_pool = SimpleNamespace(enqueue_job=AsyncMock(return_value=None))
        try:
            yield tc
        finally:
            real_app.state.arq_pool = previous_arq_pool

    real_app.dependency_overrides = previous_overrides


# ---------------------------------------------------------------------------
# DB seeding helpers
# ---------------------------------------------------------------------------


def _seed_customer_with_key(
    session: Session,
    *,
    email: str = "e2e@example.com",
    name: str = "Acme",
    webhook_secret: str | None = None,
) -> tuple[str, Customer]:
    customer = Customer(
        name=name,
        email=email,
        webhook_secret=webhook_secret,
    )
    session.add(customer)
    session.commit()
    session.refresh(customer)

    plaintext, key_hash, prefix = generate_api_key()
    key = ApiKey(
        customer_id=customer.id,  # type: ignore[arg-type]
        key_hash=key_hash,
        key_prefix=prefix,
        name="e2e-key",
    )
    session.add(key)
    session.commit()
    session.refresh(key)
    return plaintext, customer


@pytest.fixture()
def api_key(session: Session) -> str:
    plaintext, _customer = _seed_customer_with_key(session)
    return plaintext


# ---------------------------------------------------------------------------
# 1) Health endpoint
# ---------------------------------------------------------------------------


def test_health_endpoint(client: TestClient, db_engine, monkeypatch: pytest.MonkeyPatch) -> None:
    """``GET /health`` should return a valid JSON shape.

    We don't assert ``status: ok`` because ``/health`` calls the real backend
    ``/api/tags`` endpoint which isn't mocked in this test (the ``_call_backend``
    mock only kicks in on the pipeline path). The only guarantee is that the
    db subsystem reports ``ok`` given we've wired an in-memory SQLite engine.
    """
    # Point ``app.main._check_db``'s engine import at the test engine.
    import app.main as main_module

    monkeypatch.setattr("app.db.engine", db_engine)
    monkeypatch.setattr(main_module, "_check_db", _check_db_factory(db_engine))

    resp = client.get("/health")
    assert resp.status_code == 200
    body = resp.json()
    assert set(body.keys()) == {"status", "backend", "db"}
    assert body["status"] in {"ok", "degraded"}
    assert body["db"] == "ok"
    assert body["backend"] in {"ok", "unreachable"} or body["backend"].startswith("status_")


def _check_db_factory(engine):
    """Return an async ``_check_db`` bound to a specific engine."""

    async def _check_db() -> str:
        try:
            from sqlmodel import text

            with Session(engine) as session:
                session.exec(text("SELECT 1"))
            return "ok"
        except Exception:
            return "unreachable"

    return _check_db


# ---------------------------------------------------------------------------
# 2) Sync OCR returns Markdown
# ---------------------------------------------------------------------------


@needs_pdftoppm
def test_sync_ocr_returns_markdown(client: TestClient, api_key: str, session: Session) -> None:
    with SAMPLE_PDF.open("rb") as fh:
        resp = client.post(
            "/v1/ocr",
            headers={"X-API-Key": api_key},
            files={"file": ("sample.pdf", fh, "application/pdf")},
            data={"output_format": "md", "mode": "sync"},
        )

    assert resp.status_code == 200, resp.text
    assert resp.headers["content-type"] == "text/markdown; charset=utf-8"
    body = resp.text
    assert "## Seite 1" in body
    assert "## Seite 2" in body
    assert FAKE_OCR_TEXT in body

    # Job row should have been created and marked done with two pages.
    jobs = session.exec(select(Job)).all()
    assert len(jobs) == 1
    job = jobs[0]
    assert job.status == JobStatus.done
    assert job.page_count == 2
    assert job.pages_ok == 2
    assert job.pages_failed == 0


# ---------------------------------------------------------------------------
# 3) Sync OCR across all output formats
# ---------------------------------------------------------------------------


@needs_pdftoppm
@pytest.mark.parametrize(
    ("fmt", "expected_mime_prefix"),
    [
        ("md", "text/markdown"),
        ("txt", "text/plain"),
        ("toon", "application/x-toon"),
        ("json", "application/json"),
    ],
)
def test_sync_ocr_all_formats(
    client: TestClient, api_key: str, fmt: str, expected_mime_prefix: str
) -> None:
    with SAMPLE_PDF.open("rb") as fh:
        resp = client.post(
            "/v1/ocr",
            headers={"X-API-Key": api_key},
            files={"file": ("sample.pdf", fh, "application/pdf")},
            data={"output_format": fmt, "mode": "sync"},
        )

    assert resp.status_code == 200, resp.text
    assert resp.headers["content-type"].startswith(expected_mime_prefix), resp.headers[
        "content-type"
    ]

    if fmt == "json":
        payload = json.loads(resp.text)
        assert payload["meta"]["page_count"] == 2
        assert payload["meta"]["pages_ok"] == 2
        texts = {p["text"] for p in payload["pages"] if p["text"]}
        assert FAKE_OCR_TEXT in texts
    elif fmt == "txt":
        assert FAKE_OCR_TEXT in resp.text
        # Pages separated by form-feed
        assert "\f" in resp.text
    elif fmt == "toon":
        assert "page[1]:" in resp.text
        assert "page[2]:" in resp.text
    else:  # md
        assert "## Seite 1" in resp.text
        assert FAKE_OCR_TEXT in resp.text


# ---------------------------------------------------------------------------
# 4) Async flow with simulated worker (no real subprocess)
# ---------------------------------------------------------------------------


def _install_worker_subprocess_shim(monkeypatch: pytest.MonkeyPatch, db_engine) -> None:
    """Replace the worker's ``subprocess`` alias so the pipeline runs in-process.

    Also stubs out ``ensure_gpu_running`` — tests use mocked backend calls
    and never hit the real backend or the Scaleway API.

    The real worker shells out to ``python -m app.services.ocr_runner`` which
    would bypass our monkey-patched ``_call_backend``. Instead we parse the
    CLI args the worker would have used, run ``run_ocr`` directly (which
    *does* see the mocked backend), and write the JSON file the worker
    expects on the happy path — returning a ``CompletedProcess`` with
    ``returncode=0``.

    We swap only the worker module's local ``subprocess`` attribute with a
    lightweight shim so the pipeline's own ``pdfinfo`` / ``pdftoppm`` calls
    (which reference the real ``subprocess`` module, not the worker alias)
    keep working. Any non-OCR-runner call on the shim is forwarded to the
    real ``subprocess.run`` as a safety net.
    """
    # Stub out GPU auto-start — tests run without a real GPU backend.
    # Returns a dummy URL; the worker threads it into BACKEND_URL on the
    # subprocess env but the runner is shimmed out below.
    monkeypatch.setattr(
        "app.workers.ocr_worker.ensure_any_gpu_running",
        lambda: "http://test-backend:8000",
    )

    real_run = subprocess.run

    def _fake_run(cmd, check=True, timeout=None, **kwargs):  # noqa: ARG001
        is_runner_call = (
            isinstance(cmd, list)
            and len(cmd) >= 3
            and cmd[1] == "-m"
            and cmd[2] == "app.services.ocr_runner"
        )
        if not is_runner_call:
            return real_run(cmd, check=check, timeout=timeout, **kwargs)

        input_path = None
        tmp_dir = None
        output_json = None
        it = iter(cmd)
        for token in it:
            if token == "--input":
                input_path = Path(next(it))
            elif token == "--tmp-dir":
                tmp_dir = Path(next(it))
            elif token == "--output-json":
                output_json = Path(next(it))

        assert input_path is not None and tmp_dir is not None and output_json is not None

        tmp_dir.mkdir(parents=True, exist_ok=True)
        output_json.parent.mkdir(parents=True, exist_ok=True)

        result = run_ocr(input_path, tmp_dir)
        output_json.write_text(json.dumps(result.to_json_dict(), ensure_ascii=False))

        return subprocess.CompletedProcess(cmd, returncode=0, stdout=b"", stderr=b"")

    shim = SimpleNamespace(
        run=_fake_run,
        CalledProcessError=subprocess.CalledProcessError,
        TimeoutExpired=subprocess.TimeoutExpired,
        CompletedProcess=subprocess.CompletedProcess,
    )
    monkeypatch.setattr(ocr_worker_module, "subprocess", shim)
    # Point the worker's engine reference at the in-memory test engine.
    monkeypatch.setattr(ocr_worker_module, "engine", db_engine)


@needs_pdftoppm
async def test_async_ocr_flow_with_simulated_worker(
    client: TestClient,
    api_key: str,
    session: Session,
    db_engine,
    tmp_storage: LocalStorage,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """POST async, then directly invoke the worker coroutine."""
    # 1) Submit the async job
    with SAMPLE_PDF.open("rb") as fh:
        resp = client.post(
            "/v1/ocr",
            headers={"X-API-Key": api_key},
            files={"file": ("sample.pdf", fh, "application/pdf")},
            data={"output_format": "md", "mode": "async"},
        )

    assert resp.status_code == 202, resp.text
    payload = resp.json()
    job_id = payload["job_id"]
    assert payload["status"] == "pending"
    assert payload["status_url"] == f"/v1/jobs/{job_id}"

    # 2) Simulate the ARQ worker picking up the job — in-process, no subprocess
    _install_worker_subprocess_shim(monkeypatch, db_engine)

    result = await ocr_worker_module.process_ocr_job({"redis": None}, job_id)
    assert result == "done"

    # 3) Inspect DB state
    session.expire_all()
    job = session.get(Job, job_id)
    assert job is not None
    assert job.status == JobStatus.done
    assert job.page_count == 2
    assert job.pages_ok == 2
    assert job.result_path is not None
    result_file = Path(job.result_path)
    assert result_file.exists()

    # 4) GET /v1/jobs/{id}
    resp = client.get(f"/v1/jobs/{job_id}", headers={"X-API-Key": api_key})
    assert resp.status_code == 200
    status_body = resp.json()
    assert status_body["status"] == "done"
    assert status_body["page_count"] == 2
    assert status_body["result_url"] == f"/v1/jobs/{job_id}/result"

    # 5) GET /v1/jobs/{id}/result — expect markdown body with both pages
    resp = client.get(f"/v1/jobs/{job_id}/result", headers={"X-API-Key": api_key})
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("text/markdown")
    assert "## Seite 1" in resp.text
    assert "## Seite 2" in resp.text
    assert FAKE_OCR_TEXT in resp.text


# ---------------------------------------------------------------------------
# 4b) Worker marks job as failed when every page failed OCR
# ---------------------------------------------------------------------------


@needs_pdftoppm
async def test_worker_marks_job_failed_when_all_pages_fail(
    client: TestClient,
    api_key: str,
    session: Session,
    db_engine,
    tmp_storage: LocalStorage,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """If the backend rejects every page, job.status must be ``failed``.

    Prior behaviour marked the job ``done`` with ``pages_ok=0``, which
    silently returned an empty result to the caller.
    """
    with SAMPLE_PDF.open("rb") as fh:
        resp = client.post(
            "/v1/ocr",
            headers={"X-API-Key": api_key},
            files={"file": ("sample.pdf", fh, "application/pdf")},
            data={"output_format": "md", "mode": "async"},
        )
    assert resp.status_code == 202, resp.text
    job_id = resp.json()["job_id"]

    _install_worker_subprocess_shim(monkeypatch, db_engine)

    def _always_fail(img_path: Path) -> str:  # noqa: ARG001
        raise RuntimeError("backend not ready")

    monkeypatch.setattr(ocr_pipeline_module, "_call_backend", _always_fail)

    result = await ocr_worker_module.process_ocr_job({"redis": None}, job_id)
    assert result == "done"

    session.expire_all()
    job = session.get(Job, job_id)
    assert job is not None
    assert job.status == JobStatus.failed
    assert job.pages_ok == 0
    assert job.page_count > 0
    assert job.error_message is not None
    assert "alle" in job.error_message.lower() or "seiten" in job.error_message.lower()


# ---------------------------------------------------------------------------
# 5) Webhook delivery on async done
# ---------------------------------------------------------------------------


@needs_pdftoppm
async def test_webhook_delivery_on_async_done(
    client: TestClient,
    api_key: str,
    session: Session,
    db_engine,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Async job with webhook_url — after worker runs, assert POST was made."""
    # Capture every outbound webhook POST via MockTransport
    captured: dict[str, object] = {}

    def _handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        captured["headers"] = dict(request.headers)
        captured["body"] = request.content
        return httpx.Response(200, json={"ok": True})

    transport = httpx.MockTransport(_handler)

    def _factory() -> httpx.AsyncClient:
        return httpx.AsyncClient(transport=transport, timeout=5.0)

    monkeypatch.setattr(webhook_module, "_client_factory", _factory)
    # Webhook code queries the DB via the module-level engine — point it at ours.
    monkeypatch.setattr(webhook_module, "engine", db_engine)

    # Submit async with webhook URL
    with SAMPLE_PDF.open("rb") as fh:
        resp = client.post(
            "/v1/ocr",
            headers={"X-API-Key": api_key},
            files={"file": ("sample.pdf", fh, "application/pdf")},
            data={
                "output_format": "md",
                "mode": "async",
                "webhook_url": "http://fake-webhook.test/callback",
            },
        )
    assert resp.status_code == 202
    job_id = resp.json()["job_id"]

    # Run the worker in-process
    _install_worker_subprocess_shim(monkeypatch, db_engine)
    result = await ocr_worker_module.process_ocr_job({"redis": None}, job_id)
    assert result == "done"

    # Webhook should have been delivered exactly once
    assert "url" in captured, "webhook was not called"
    assert captured["url"] == "http://fake-webhook.test/callback"

    body = json.loads(captured["body"])  # type: ignore[arg-type]
    assert body["job_id"] == job_id
    assert body["status"] == "done"
    assert body["result_url"] == f"/v1/jobs/{job_id}/result"
    assert body["page_count"] == 2

    # DB should now reflect successful delivery
    session.expire_all()
    job = session.get(Job, job_id)
    assert job is not None
    assert job.webhook_delivered is True
    assert job.webhook_attempts == 1


# ---------------------------------------------------------------------------
# 6) Rejects bad MIME
# ---------------------------------------------------------------------------


def test_rejects_bad_mime(client: TestClient, api_key: str) -> None:
    resp = client.post(
        "/v1/ocr",
        headers={"X-API-Key": api_key},
        files={"file": ("note.txt", b"just some text, not a pdf", "text/plain")},
        data={"output_format": "md", "mode": "sync"},
    )
    assert resp.status_code == 415
    assert "Unsupported media type" in resp.json()["detail"]


# ---------------------------------------------------------------------------
# 7) Rejects missing API key
# ---------------------------------------------------------------------------


def test_rejects_missing_api_key(client: TestClient) -> None:
    resp = client.post(
        "/v1/ocr",
        files={"file": ("doc.pdf", b"%PDF-1.4\n", "application/pdf")},
        data={"output_format": "md", "mode": "sync"},
    )
    # FastAPI's Header(...) returns 422 for a missing required header; our
    # ``require_api_key`` also raises 401 when the header is an empty string.
    # Both should be considered a rejection.
    assert resp.status_code in (401, 422)


# ---------------------------------------------------------------------------
# 8) List own jobs only — cross-tenant isolation
# ---------------------------------------------------------------------------


@needs_pdftoppm
def test_list_own_jobs_only(client: TestClient, session: Session) -> None:
    key_a, _customer_a = _seed_customer_with_key(session, email="a-e2e@example.com", name="A")
    key_b, _customer_b = _seed_customer_with_key(session, email="b-e2e@example.com", name="B")

    # Submit one sync job per customer
    with SAMPLE_PDF.open("rb") as fh:
        resp_a = client.post(
            "/v1/ocr",
            headers={"X-API-Key": key_a},
            files={"file": ("a.pdf", fh, "application/pdf")},
            data={"output_format": "md", "mode": "sync"},
        )
    assert resp_a.status_code == 200, resp_a.text

    with SAMPLE_PDF.open("rb") as fh:
        resp_b = client.post(
            "/v1/ocr",
            headers={"X-API-Key": key_b},
            files={"file": ("b.pdf", fh, "application/pdf")},
            data={"output_format": "md", "mode": "sync"},
        )
    assert resp_b.status_code == 200, resp_b.text

    # Each caller only sees their own job
    list_a = client.get("/v1/jobs", headers={"X-API-Key": key_a}).json()
    assert list_a["total"] == 1
    assert len(list_a["items"]) == 1
    assert list_a["items"][0]["status"] == "done"

    list_b = client.get("/v1/jobs", headers={"X-API-Key": key_b}).json()
    assert list_b["total"] == 1
    assert len(list_b["items"]) == 1
    assert list_b["items"][0]["status"] == "done"

    # And the two job IDs are distinct
    assert list_a["items"][0]["job_id"] != list_b["items"][0]["job_id"]
