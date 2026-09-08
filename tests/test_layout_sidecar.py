"""Layout-Sidecar (A45, DocklyStorage-Integration): ``layout.txt`` je
opendataloader-Job — die Ausgabe von ``pdftotext -layout``, also der
Text MIT seinen Spaltenpositionen.

Warum das noetig ist: opendataloader liefert fuer DATEV-artige Auswertungen
(BWA ohne Tabellenlinien) keine Tabelle, sondern Fliesstext — die Zahlen
einer Zeile ohne ihre Spalten. Erst die Zeichenpositionen aus dem Layout
erlauben, jede Zahl ihrer Ueberschrift zuzuordnen, auch bei leeren Zellen.
Deterministisch (poppler), kein Modell — passt zum Pin auf opendataloader.
"""

from __future__ import annotations

import shutil
import subprocess
from collections.abc import Iterator
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

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
from app.services.opendataloader_pipeline import run_opendataloader
from app.services.storage import LocalStorage
from app.workers import ocr_worker as ocr_worker_module

FIXTURES_DIR = Path(__file__).parent / "fixtures"
SAMPLE_PDF = FIXTURES_DIR / "sample.pdf"

needs_pdftotext = pytest.mark.skipif(
    shutil.which("pdftotext") is None, reason="pdftotext (poppler) nicht verfuegbar"
)


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
    fastapi_app.include_router(jobs_router_module.router, prefix="/v1")
    return fastapi_app


@pytest.fixture()
def client(app: FastAPI) -> TestClient:
    return TestClient(app)


@pytest.fixture()
def api_key(session: Session) -> str:
    customer = Customer(name="Layout GmbH", email="layout@example.com")
    session.add(customer)
    session.commit()
    session.refresh(customer)
    plaintext, key_hash, prefix = generate_api_key()
    session.add(
        ApiKey(
            customer_id=customer.id,  # type: ignore[arg-type]
            key_hash=key_hash,
            key_prefix=prefix,
            name="layout",
        )
    )
    session.commit()
    return plaintext


def _seed_done_job(session: Session, api_key_plain: str, *, engine: str) -> Job:
    key = session.exec(select(ApiKey).where(ApiKey.key_hash == hash_api_key(api_key_plain))).one()
    job = Job(
        api_key_id=key.id,  # type: ignore[arg-type]
        customer_id=key.customer_id,
        status=JobStatus.done,
        input_filename="bwa.pdf",
        input_size_bytes=9,
        input_mime="application/pdf",
        output_format=OutputFormat.md,
        engine=engine,
        requested_engine="opendataloader",
    )
    session.add(job)
    session.commit()
    session.refresh(job)
    return job


# --- Pipeline: Sidecar wird geschrieben ---------------------------------------


def _fake_convert_factory(markdown_body: str, output_stem: str):
    def _fake_convert(input_path, output_dir, **_kwargs):  # noqa: ANN001, ARG001
        out = Path(output_dir)
        out.mkdir(parents=True, exist_ok=True)
        (out / f"{output_stem}.md").write_text(markdown_body, encoding="utf-8")

    return _fake_convert


@needs_pdftotext
def test_run_opendataloader_writes_layout_sidecar(tmp_path: Path) -> None:
    layout_path = tmp_path / "out" / "layout.txt"
    with patch("opendataloader_pdf.convert", _fake_convert_factory("# Seite\n", "sample")):
        run_opendataloader(SAMPLE_PDF, tmp_path / "work", layout_path=layout_path)
    assert layout_path.exists()


def test_run_opendataloader_without_layout_path_writes_nothing(tmp_path: Path) -> None:
    with patch("opendataloader_pdf.convert", _fake_convert_factory("# Seite\n", "sample")):
        run_opendataloader(SAMPLE_PDF, tmp_path / "work")
    assert not (tmp_path / "work" / "layout.txt").exists()
    assert list(tmp_path.glob("**/layout.txt")) == []


# --- Jobs-API: layout_url + Download -------------------------------------------


def test_job_detail_exposes_layout_url_when_sidecar_exists(
    client: TestClient, api_key: str, session: Session, tmp_storage: LocalStorage
) -> None:
    job = _seed_done_job(session, api_key, engine="opendataloader")
    tmp_storage.save_layout(job.id, b"Position      EUR\nUmsatz     1.234\n")

    resp = client.get(f"/v1/jobs/{job.id}", headers={"X-API-Key": api_key})

    assert resp.status_code == 200
    assert resp.json()["layout_url"] == f"/v1/jobs/{job.id}/layout"


def test_job_detail_has_no_layout_url_without_sidecar(
    client: TestClient, api_key: str, session: Session
) -> None:
    job = _seed_done_job(session, api_key, engine="vllm")
    resp = client.get(f"/v1/jobs/{job.id}", headers={"X-API-Key": api_key})
    assert resp.status_code == 200
    assert resp.json()["layout_url"] is None


def test_get_layout_returns_plain_text(
    client: TestClient, api_key: str, session: Session, tmp_storage: LocalStorage
) -> None:
    job = _seed_done_job(session, api_key, engine="opendataloader")
    body = "Position      EUR\nUmsatz     1.234\n\x0c"
    tmp_storage.save_layout(job.id, body.encode("utf-8"))

    resp = client.get(f"/v1/jobs/{job.id}/layout", headers={"X-API-Key": api_key})

    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("text/plain")
    assert resp.text == body


def test_get_layout_404_without_sidecar(client: TestClient, api_key: str, session: Session) -> None:
    job = _seed_done_job(session, api_key, engine="vllm")
    resp = client.get(f"/v1/jobs/{job.id}/layout", headers={"X-API-Key": api_key})
    assert resp.status_code == 404


# --- Worker: Runner bekommt --layout-path bei opendataloader ------------------


async def test_worker_passes_layout_path_for_opendataloader(
    session: Session, api_key: str, tmp_storage: LocalStorage, db_engine, monkeypatch
) -> None:
    key = session.exec(select(ApiKey).where(ApiKey.key_hash == hash_api_key(api_key))).one()
    job = Job(
        api_key_id=key.id,  # type: ignore[arg-type]
        customer_id=key.customer_id,
        status=JobStatus.pending,
        input_filename="bwa.pdf",
        input_size_bytes=9,
        input_mime="application/pdf",
        output_format=OutputFormat.md,
        requested_engine="opendataloader",
    )
    session.add(job)
    session.commit()
    session.refresh(job)
    tmp_storage.save_upload(job.id, "bwa.pdf", b"%PDF-1.4\n")

    seen: list[list[str]] = []

    def _fake_run(cmd, check=True, timeout=None, **kwargs):  # noqa: ARG001
        seen.append(list(cmd))
        return subprocess.CompletedProcess(cmd, returncode=1, stdout=b"", stderr=b"")

    shim = SimpleNamespace(
        run=_fake_run,
        CalledProcessError=subprocess.CalledProcessError,
        TimeoutExpired=subprocess.TimeoutExpired,
        CompletedProcess=subprocess.CompletedProcess,
    )
    monkeypatch.setattr(ocr_worker_module, "subprocess", shim)
    monkeypatch.setattr(ocr_worker_module, "engine", db_engine)

    await ocr_worker_module.process_ocr_job({"redis": None}, job.id)

    assert seen, "Runner wurde nicht aufgerufen"
    cmd = seen[0]
    assert "--layout-path" in cmd
    assert cmd[cmd.index("--layout-path") + 1].endswith("layout.txt")
