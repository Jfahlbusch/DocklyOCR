"""A45 (DocklyStorage-Integration): den OCR-Motor beim Einreichen festlegen.

``engine=opendataloader`` pinnt den CPU-Parser und schaltet BEIDES ab, was
sonst automatisch passiert — den vLLM-Fallback und den GPU-Start. Ein PDF
ohne Textebene scheitert dann laut, statt still GPU-Zeit zu kosten. ``auto``
bleibt das bisherige Verhalten (Router + Fallback), ``vllm`` erzwingt die
Vision-Pipeline.

Die Worker-Tests ersetzen ``subprocess`` durch einen Shim (wie test_e2e.py),
der die Exit-Codes vorgibt und ``result.json`` schreibt. Der GPU-Start ist
in den Pin-Tests eine Assertion: wird er trotz Pin aufgerufen, ist genau das
der Fehler, den diese Tests verhindern sollen.
"""

from __future__ import annotations

import io
import json
import subprocess
from collections.abc import Iterator
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy.pool import StaticPool
from sqlmodel import Session, SQLModel, create_engine, select

from app.auth import generate_api_key, hash_api_key, rate_limiter
from app.db import get_session
from app.models import ApiKey, Customer, Job, JobStatus, OutputFormat
from app.routers import jobs as jobs_router_module
from app.routers import ocr as ocr_router_module
from app.services import storage as storage_module
from app.services.ocr_pipeline import OcrResult, PageResult
from app.services.ocr_runner import EXIT_OPENDATALOADER_UNACCEPTABLE
from app.services.storage import LocalStorage
from app.workers import ocr_worker as ocr_worker_module

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
def _reset_rate_limiter() -> Iterator[None]:
    """Der Limiter ist ein prozessweiter Singleton und zaehlt je Schluessel-ID.
    In-Memory-SQLite vergibt in jeder Testdatei wieder die ID 1 — ohne Reset
    zehren die Anfragen hier das Budget der NAECHSTEN Testdatei auf (429 dort,
    obwohl der Fehler hier liegt). Dasselbe Muster wie test_e2e.py."""
    rate_limiter.reset()
    yield
    rate_limiter.reset()


@pytest.fixture(autouse=True)
def tmp_storage(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> LocalStorage:
    new_storage = LocalStorage(tmp_path / "storage")
    monkeypatch.setattr(storage_module, "storage", new_storage)
    monkeypatch.setattr(ocr_router_module, "storage", new_storage)
    monkeypatch.setattr(jobs_router_module, "storage", new_storage)
    monkeypatch.setattr(ocr_worker_module, "storage", new_storage)
    return new_storage


@pytest.fixture()
def app(db_engine) -> FastAPI:
    fastapi_app = FastAPI()
    fastapi_app.state.arq_pool = SimpleNamespace(enqueue_job=AsyncMock(return_value=None))

    def _override_session() -> Iterator[Session]:
        with Session(db_engine) as s:
            yield s

    fastapi_app.dependency_overrides[get_session] = _override_session
    fastapi_app.include_router(ocr_router_module.router, prefix="/v1")
    fastapi_app.include_router(jobs_router_module.router, prefix="/v1")
    return fastapi_app


@pytest.fixture()
def client(app: FastAPI) -> TestClient:
    return TestClient(app)


@pytest.fixture()
def api_key(session: Session) -> str:
    customer = Customer(name="Pin GmbH", email="pin@example.com")
    session.add(customer)
    session.commit()
    session.refresh(customer)
    plaintext, key_hash, prefix = generate_api_key()
    session.add(
        ApiKey(
            customer_id=customer.id,  # type: ignore[arg-type]
            key_hash=key_hash,
            key_prefix=prefix,
            name="pin",
        )
    )
    session.commit()
    return plaintext


def _submit(client: TestClient, api_key: str, **data: str) -> dict:
    resp = client.post(
        "/v1/ocr",
        headers={"X-API-Key": api_key},
        files={"file": ("bwa.pdf", io.BytesIO(b"%PDF-1.4\n"), "application/pdf")},
        data={"output_format": "md", "mode": "async", **data},
    )
    return {"status": resp.status_code, "json": resp.json()}


# --- Einreichen ------------------------------------------------------------


def test_submit_stores_requested_engine(client: TestClient, api_key: str, session: Session) -> None:
    out = _submit(client, api_key, engine="opendataloader")
    assert out["status"] == 202, out
    job = session.get(Job, out["json"]["job_id"])
    assert job is not None
    assert job.requested_engine == "opendataloader"


def test_submit_defaults_to_auto(client: TestClient, api_key: str, session: Session) -> None:
    # Ohne Angabe bleibt alles wie bisher — bestehende Clients merken nichts.
    out = _submit(client, api_key)
    assert out["status"] == 202, out
    job = session.get(Job, out["json"]["job_id"])
    assert job is not None
    assert job.requested_engine == "auto"


def test_submit_rejects_unknown_engine(client: TestClient, api_key: str) -> None:
    out = _submit(client, api_key, engine="magic")
    # FastAPI validiert das Literal selbst (422); ein 400 wäre ebenso korrekt.
    assert out["status"] in (400, 422), out


def test_batch_stores_requested_engine(client: TestClient, api_key: str, session: Session) -> None:
    resp = client.post(
        "/v1/ocr/batch",
        headers={"X-API-Key": api_key},
        files=[
            ("files", ("a.pdf", io.BytesIO(b"%PDF-1.4\n"), "application/pdf")),
            ("files", ("b.pdf", io.BytesIO(b"%PDF-1.4\n"), "application/pdf")),
        ],
        data={"output_format": "md", "engine": "opendataloader"},
    )
    assert resp.status_code == 202, resp.text
    for entry in resp.json()["jobs"]:
        job = session.get(Job, entry["job_id"])
        assert job is not None
        assert job.requested_engine == "opendataloader"


def test_job_detail_exposes_requested_engine(
    client: TestClient, api_key: str, session: Session
) -> None:
    # DocklyStorage prüft nach dem Lauf, ob wirklich der verlangte Motor lief.
    out = _submit(client, api_key, engine="opendataloader")
    resp = client.get(f"/v1/jobs/{out['json']['job_id']}", headers={"X-API-Key": api_key})
    assert resp.status_code == 200
    assert resp.json()["requested_engine"] == "opendataloader"


# --- Worker ----------------------------------------------------------------


def _seed_job(
    session: Session, api_key_plain: str, storage: LocalStorage, *, requested_engine: str
) -> str:
    key = session.exec(select(ApiKey).where(ApiKey.key_hash == hash_api_key(api_key_plain))).one()
    job = Job(
        api_key_id=key.id,  # type: ignore[arg-type]
        customer_id=key.customer_id,
        status=JobStatus.pending,
        input_filename="bwa.pdf",
        input_size_bytes=9,
        input_mime="application/pdf",
        output_format=OutputFormat.md,
        requested_engine=requested_engine,
    )
    session.add(job)
    session.commit()
    session.refresh(job)
    storage.save_upload(job.id, "bwa.pdf", b"%PDF-1.4\n")
    return job.id


def _gpu_forbidden() -> tuple[str, str]:
    raise AssertionError("GPU darf bei erzwungenem opendataloader NICHT gestartet werden")


def _gpu_allowed() -> tuple[str, str]:
    return ("http://test-backend:8000", "test-instance")


def _install_shim(
    monkeypatch: pytest.MonkeyPatch, db_engine, *, returncodes: list[int], gpu
) -> list[str]:
    """Ersetzt den Runner-Subprozess: gibt die Exit-Codes der Reihe nach zurück
    und schreibt bei 0 ein result.json. Liefert die Liste der verlangten Motoren."""
    engines_called: list[str] = []

    def _fake_run(cmd, check=True, timeout=None, **kwargs):  # noqa: ARG001
        engine_arg = None
        output_json = None
        it = iter(cmd)
        for token in it:
            if token == "--engine":
                engine_arg = next(it)
            elif token == "--output-json":
                output_json = Path(next(it))
        assert engine_arg is not None and output_json is not None
        engines_called.append(engine_arg)
        rc = returncodes.pop(0)
        if rc == 0:
            output_json.parent.mkdir(parents=True, exist_ok=True)
            result = OcrResult(
                pages=[
                    PageResult(
                        number=1,
                        text="|Position|Betrag|\n|---|---|\n|Umsatz|1.000,00 EUR|",
                        strategy=engine_arg,
                        elapsed_s=0.01,
                    )
                ],
                page_count=1,
                pages_ok=1,
                pages_failed=0,
            )
            output_json.write_text(json.dumps(result.to_json_dict(), ensure_ascii=False))
        return subprocess.CompletedProcess(cmd, returncode=rc, stdout=b"", stderr=b"")

    shim = SimpleNamespace(
        run=_fake_run,
        CalledProcessError=subprocess.CalledProcessError,
        TimeoutExpired=subprocess.TimeoutExpired,
        CompletedProcess=subprocess.CompletedProcess,
    )
    monkeypatch.setattr(ocr_worker_module, "subprocess", shim)
    monkeypatch.setattr(ocr_worker_module, "engine", db_engine)
    monkeypatch.setattr(ocr_worker_module, "ensure_any_gpu_running", gpu)
    return engines_called


async def test_pinned_opendataloader_overrides_router_and_never_starts_gpu(
    session: Session, api_key: str, tmp_storage: LocalStorage, db_engine, monkeypatch
) -> None:
    # Der Router würde vLLM wählen — der Pin muss gewinnen.
    monkeypatch.setattr(ocr_worker_module, "select_engine", lambda _p: "vllm")
    job_id = _seed_job(session, api_key, tmp_storage, requested_engine="opendataloader")
    called = _install_shim(monkeypatch, db_engine, returncodes=[0], gpu=_gpu_forbidden)

    result = await ocr_worker_module.process_ocr_job({"redis": None}, job_id)

    assert result == "done"
    assert called == ["opendataloader"]
    session.expire_all()
    job = session.get(Job, job_id)
    assert job is not None
    assert job.status == JobStatus.done
    assert job.engine == "opendataloader"


async def test_pinned_opendataloader_fails_loudly_instead_of_falling_back(
    session: Session, api_key: str, tmp_storage: LocalStorage, db_engine, monkeypatch
) -> None:
    # Exit-Code 2 = "zu wenig Text" (Scan). Ohne Pin folgte hier der
    # vLLM-Fallback samt GPU-Start; mit Pin ist es ein klarer Fehlschlag.
    monkeypatch.setattr(ocr_worker_module, "select_engine", lambda _p: "opendataloader")
    job_id = _seed_job(session, api_key, tmp_storage, requested_engine="opendataloader")
    called = _install_shim(
        monkeypatch, db_engine, returncodes=[EXIT_OPENDATALOADER_UNACCEPTABLE], gpu=_gpu_forbidden
    )

    result = await ocr_worker_module.process_ocr_job({"redis": None}, job_id)

    assert result == "opendataloader_unacceptable"
    assert called == ["opendataloader"]
    session.expire_all()
    job = session.get(Job, job_id)
    assert job is not None
    assert job.status == JobStatus.failed
    assert job.error_message is not None
    assert "Textebene" in job.error_message
    assert "opendataloader" in job.error_message


async def test_auto_keeps_the_vllm_fallback(
    session: Session, api_key: str, tmp_storage: LocalStorage, db_engine, monkeypatch
) -> None:
    # Bisheriges Verhalten bleibt für alle, die nichts angeben.
    monkeypatch.setattr(ocr_worker_module, "select_engine", lambda _p: "opendataloader")
    job_id = _seed_job(session, api_key, tmp_storage, requested_engine="auto")
    called = _install_shim(
        monkeypatch, db_engine, returncodes=[EXIT_OPENDATALOADER_UNACCEPTABLE, 0], gpu=_gpu_allowed
    )

    result = await ocr_worker_module.process_ocr_job({"redis": None}, job_id)

    assert result == "done"
    assert called == ["opendataloader", "vllm"]
    session.expire_all()
    job = session.get(Job, job_id)
    assert job is not None
    assert job.engine == "vllm-fallback-after-opendataloader"


async def test_pinned_vllm_forces_the_vision_pipeline(
    session: Session, api_key: str, tmp_storage: LocalStorage, db_engine, monkeypatch
) -> None:
    monkeypatch.setattr(ocr_worker_module, "select_engine", lambda _p: "opendataloader")
    job_id = _seed_job(session, api_key, tmp_storage, requested_engine="vllm")
    called = _install_shim(monkeypatch, db_engine, returncodes=[0], gpu=_gpu_allowed)

    result = await ocr_worker_module.process_ocr_job({"redis": None}, job_id)

    assert result == "done"
    assert called == ["vllm"]
    session.expire_all()
    job = session.get(Job, job_id)
    assert job is not None
    assert job.engine == "vllm"
