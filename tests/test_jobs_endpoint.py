"""Tests for `app/routers/jobs.py` — GET /v1/jobs, /v1/jobs/{id}, /v1/jobs/{id}/result."""

from __future__ import annotations

from collections.abc import Iterator
from datetime import datetime, timedelta
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy.pool import StaticPool
from sqlmodel import Session, SQLModel, create_engine

from app.auth import generate_api_key
from app.db import get_session
from app.models import ApiKey, Customer, Job, JobStatus, OutputFormat
from app.routers import jobs as jobs_router_module
from app.services import storage as storage_module
from app.services.storage import LocalStorage

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
def _patch_storage(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> LocalStorage:
    new_storage = LocalStorage(tmp_path / "test_storage")
    monkeypatch.setattr(storage_module, "storage", new_storage)
    monkeypatch.setattr(jobs_router_module, "storage", new_storage)
    return new_storage


@pytest.fixture()
def app(db_engine) -> FastAPI:
    fastapi_app = FastAPI()

    def _override_session() -> Iterator[Session]:
        with Session(db_engine) as s:
            yield s

    fastapi_app.dependency_overrides[get_session] = _override_session
    fastapi_app.include_router(jobs_router_module.router, prefix="/v1")
    return fastapi_app


@pytest.fixture()
def client(app: FastAPI) -> TestClient:
    return TestClient(app)


def _seed_customer(session: Session, email: str = "a@example.com") -> tuple[str, Customer]:
    customer = Customer(name="Acme", email=email)
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
    session.refresh(key)
    return plaintext, customer


def _seed_job(
    session: Session,
    customer: Customer,
    *,
    status: JobStatus = JobStatus.done,
    output_format: OutputFormat = OutputFormat.md,
    filename: str = "doc.pdf",
    created_at: datetime | None = None,
) -> Job:
    key = session.exec(
        __import__("sqlmodel").select(ApiKey).where(ApiKey.customer_id == customer.id)
    ).first()
    assert key is not None

    kwargs = {
        "api_key_id": key.id,
        "customer_id": customer.id,
        "status": status,
        "input_filename": filename,
        "input_size_bytes": 1024,
        "input_mime": "application/pdf",
        "output_format": output_format,
    }
    if status == JobStatus.done:
        kwargs.update(
            {
                "page_count": 3,
                "pages_ok": 3,
                "pages_failed": 0,
                "finished_at": datetime.utcnow(),
                "started_at": datetime.utcnow() - timedelta(seconds=10),
                "result_mime": "text/markdown; charset=utf-8",
            }
        )
    if created_at is not None:
        kwargs["created_at"] = created_at

    job = Job(**kwargs)
    session.add(job)
    session.commit()
    session.refresh(job)
    return job


# --- GET /v1/jobs/{id} -----------------------------------------------------


def test_get_job_own(client: TestClient, session: Session) -> None:
    api_key, customer = _seed_customer(session)
    job = _seed_job(session, customer)

    resp = client.get(f"/v1/jobs/{job.id}", headers={"X-API-Key": api_key})
    assert resp.status_code == 200
    body = resp.json()
    assert body["job_id"] == job.id
    assert body["status"] == "done"
    assert body["output_format"] == "md"
    assert body["result_url"] == f"/v1/jobs/{job.id}/result"
    assert body["page_count"] == 3
    assert body["pages_ok"] == 3


def test_get_job_not_found(client: TestClient, session: Session) -> None:
    api_key, _customer = _seed_customer(session)
    resp = client.get("/v1/jobs/doesnotexist123", headers={"X-API-Key": api_key})
    assert resp.status_code == 404


def test_get_job_other_customer_returns_404(client: TestClient, session: Session) -> None:
    _a_key, customer_a = _seed_customer(session, email="a@example.com")
    b_key, customer_b = _seed_customer(session, email="b@example.com")
    job_a = _seed_job(session, customer_a)

    # Customer B tries to read Customer A's job
    resp = client.get(f"/v1/jobs/{job_a.id}", headers={"X-API-Key": b_key})
    assert resp.status_code == 404


def test_get_job_no_result_url_when_pending(client: TestClient, session: Session) -> None:
    api_key, customer = _seed_customer(session)
    job = _seed_job(session, customer, status=JobStatus.pending)

    resp = client.get(f"/v1/jobs/{job.id}", headers={"X-API-Key": api_key})
    assert resp.status_code == 200
    assert resp.json()["result_url"] is None


# --- GET /v1/jobs (list) ---------------------------------------------------


def test_list_jobs_empty(client: TestClient, session: Session) -> None:
    api_key, _customer = _seed_customer(session)
    resp = client.get("/v1/jobs", headers={"X-API-Key": api_key})
    assert resp.status_code == 200
    body = resp.json()
    assert body["items"] == []
    assert body["total"] == 0
    assert body["limit"] == 20
    assert body["offset"] == 0


def test_list_jobs_returns_own_only(client: TestClient, session: Session) -> None:
    a_key, customer_a = _seed_customer(session, email="a@example.com")
    _b_key, customer_b = _seed_customer(session, email="b@example.com")

    _seed_job(session, customer_a, filename="a1.pdf")
    _seed_job(session, customer_a, filename="a2.pdf")
    _seed_job(session, customer_b, filename="b1.pdf")

    resp = client.get("/v1/jobs", headers={"X-API-Key": a_key})
    assert resp.status_code == 200
    body = resp.json()
    assert body["total"] == 2
    assert len(body["items"]) == 2


def test_list_jobs_pagination(client: TestClient, session: Session) -> None:
    api_key, customer = _seed_customer(session)
    for i in range(5):
        _seed_job(
            session,
            customer,
            filename=f"doc{i}.pdf",
            created_at=datetime.utcnow() - timedelta(minutes=5 - i),
        )

    resp = client.get("/v1/jobs?limit=2&offset=0", headers={"X-API-Key": api_key})
    assert resp.status_code == 200
    body = resp.json()
    assert body["total"] == 5
    assert len(body["items"]) == 2
    assert body["limit"] == 2

    resp2 = client.get("/v1/jobs?limit=2&offset=2", headers={"X-API-Key": api_key})
    assert resp2.status_code == 200
    assert len(resp2.json()["items"]) == 2

    resp3 = client.get("/v1/jobs?limit=2&offset=4", headers={"X-API-Key": api_key})
    assert resp3.status_code == 200
    assert len(resp3.json()["items"]) == 1


def test_list_jobs_status_filter(client: TestClient, session: Session) -> None:
    api_key, customer = _seed_customer(session)
    _seed_job(session, customer, status=JobStatus.done)
    _seed_job(session, customer, status=JobStatus.done)
    _seed_job(session, customer, status=JobStatus.pending)
    _seed_job(session, customer, status=JobStatus.failed)

    resp = client.get("/v1/jobs?status=done", headers={"X-API-Key": api_key})
    assert resp.status_code == 200
    body = resp.json()
    assert body["total"] == 2
    assert all(item["status"] == "done" for item in body["items"])

    resp2 = client.get("/v1/jobs?status=pending", headers={"X-API-Key": api_key})
    assert resp2.status_code == 200
    assert resp2.json()["total"] == 1


def test_list_jobs_limit_bounds(client: TestClient, session: Session) -> None:
    api_key, _customer = _seed_customer(session)
    # limit > 100 should be rejected
    resp = client.get("/v1/jobs?limit=500", headers={"X-API-Key": api_key})
    assert resp.status_code == 422


# --- GET /v1/jobs/{id}/result ----------------------------------------------


def test_get_result_returns_file(
    client: TestClient, session: Session, _patch_storage: LocalStorage
) -> None:
    api_key, customer = _seed_customer(session)
    job = _seed_job(session, customer, output_format=OutputFormat.md)
    _patch_storage.save_result(job.id, b"# Hello World", "md")

    resp = client.get(f"/v1/jobs/{job.id}/result", headers={"X-API-Key": api_key})
    assert resp.status_code == 200
    assert resp.content == b"# Hello World"
    assert "attachment" in resp.headers["content-disposition"]
    assert 'filename="doc.md"' in resp.headers["content-disposition"]


def test_get_structure_returns_json_sidecar_for_opendataloader(
    client: TestClient, session: Session, _patch_storage: LocalStorage
) -> None:
    """opendataloader-served jobs expose a JSON sidecar via /structure."""
    import json as jsonlib

    api_key, customer = _seed_customer(session)
    job = _seed_job(session, customer)
    # Mark this as opendataloader-served and drop a structure file
    job.engine = "opendataloader"
    session.add(job)
    session.commit()
    sidecar = {"elements": [{"type": "heading", "page number": 1, "bounding box": [1, 2, 3, 4]}]}
    _patch_storage.save_structure(job.id, jsonlib.dumps(sidecar).encode("utf-8"))

    resp = client.get(f"/v1/jobs/{job.id}/structure", headers={"X-API-Key": api_key})

    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("application/json")
    assert jsonlib.loads(resp.content)["elements"][0]["type"] == "heading"


def test_get_preview_returns_html_sidecar_for_opendataloader(
    client: TestClient, session: Session, _patch_storage: LocalStorage
) -> None:
    """opendataloader-served jobs expose an HTML preview via /preview."""
    api_key, customer = _seed_customer(session)
    job = _seed_job(session, customer)
    job.engine = "opendataloader"
    session.add(job)
    session.commit()
    _patch_storage.save_preview(job.id, b"<!doctype html><html><body>preview</body></html>")

    resp = client.get(f"/v1/jobs/{job.id}/preview", headers={"X-API-Key": api_key})

    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("text/html")
    # inline disposition so a browser renders the preview directly
    assert resp.headers["content-disposition"].startswith("inline")
    assert b"preview" in resp.content


def test_job_detail_response_exposes_preview_url(
    client: TestClient, session: Session, _patch_storage: LocalStorage
) -> None:
    api_key, customer = _seed_customer(session)
    job = _seed_job(session, customer)
    job.engine = "opendataloader"
    session.add(job)
    session.commit()
    _patch_storage.save_result(job.id, b"# md", "md")
    _patch_storage.save_preview(job.id, b"<html></html>")

    resp = client.get(f"/v1/jobs/{job.id}", headers={"X-API-Key": api_key})

    body = resp.json()
    assert body["preview_url"] == f"/v1/jobs/{job.id}/preview"


def test_get_preview_404_when_no_sidecar(
    client: TestClient, session: Session, _patch_storage: LocalStorage
) -> None:
    api_key, customer = _seed_customer(session)
    job = _seed_job(session, customer)
    job.engine = "vllm"
    session.add(job)
    session.commit()
    _patch_storage.save_result(job.id, b"# md", "md")

    resp = client.get(f"/v1/jobs/{job.id}/preview", headers={"X-API-Key": api_key})
    assert resp.status_code == 404


def test_get_structure_404_when_no_sidecar(
    client: TestClient, session: Session, _patch_storage: LocalStorage
) -> None:
    """vLLM-served jobs (or any without a sidecar) return 404."""
    api_key, customer = _seed_customer(session)
    job = _seed_job(session, customer)
    job.engine = "vllm"
    session.add(job)
    session.commit()
    _patch_storage.save_result(job.id, b"# md", "md")

    resp = client.get(f"/v1/jobs/{job.id}/structure", headers={"X-API-Key": api_key})

    assert resp.status_code == 404
    assert "structure" in resp.json()["detail"].lower()


def test_job_detail_response_exposes_structure_url_when_present(
    client: TestClient, session: Session, _patch_storage: LocalStorage
) -> None:
    import json as jsonlib

    api_key, customer = _seed_customer(session)
    job = _seed_job(session, customer)
    job.engine = "opendataloader"
    session.add(job)
    session.commit()
    _patch_storage.save_result(job.id, b"# md", "md")
    _patch_storage.save_structure(job.id, jsonlib.dumps({"elements": []}).encode("utf-8"))

    resp = client.get(f"/v1/jobs/{job.id}", headers={"X-API-Key": api_key})

    assert resp.status_code == 200
    body = resp.json()
    assert body["structure_url"] == f"/v1/jobs/{job.id}/structure"
    assert body["engine"] == "opendataloader"


def test_get_result_umlaut_filename_does_not_500(
    client: TestClient, session: Session, _patch_storage: LocalStorage
) -> None:
    """Regression: macOS NFD filenames (ä = a + U+0308) used to crash the
    result endpoint with UnicodeEncodeError on the Content-Disposition
    header → HTTP 500 for every job whose name had an umlaut."""
    api_key, customer = _seed_customer(session)
    # NFD form — exactly what a macOS upload sends
    nfd_name = "Versicherungsbestätigung.pdf"
    job = _seed_job(session, customer, output_format=OutputFormat.md, filename=nfd_name)
    _patch_storage.save_result(job.id, b"# Done", "md")

    resp = client.get(f"/v1/jobs/{job.id}/result", headers={"X-API-Key": api_key})

    assert resp.status_code == 200
    assert resp.content == b"# Done"
    cd = resp.headers["content-disposition"]
    assert "attachment" in cd
    # RFC 5987 UTF-8 form must be present for the non-ASCII name
    assert "filename*=UTF-8''" in cd
    # The header must at minimum be latin-1 constructible (no 500)
    cd.encode("latin-1")


def test_get_result_not_ready_returns_409(client: TestClient, session: Session) -> None:
    api_key, customer = _seed_customer(session)
    job = _seed_job(session, customer, status=JobStatus.pending)

    resp = client.get(f"/v1/jobs/{job.id}/result", headers={"X-API-Key": api_key})
    assert resp.status_code == 409
    assert "not ready" in resp.json()["detail"].lower()


def test_get_result_missing_file_returns_410(client: TestClient, session: Session) -> None:
    api_key, customer = _seed_customer(session)
    job = _seed_job(session, customer, status=JobStatus.done)
    # Intentionally do NOT write a result file

    resp = client.get(f"/v1/jobs/{job.id}/result", headers={"X-API-Key": api_key})
    assert resp.status_code == 410


def test_get_result_other_customer_returns_404(
    client: TestClient, session: Session, _patch_storage: LocalStorage
) -> None:
    _a_key, customer_a = _seed_customer(session, email="a@example.com")
    b_key, _customer_b = _seed_customer(session, email="b@example.com")
    job_a = _seed_job(session, customer_a)
    _patch_storage.save_result(job_a.id, b"secret", "md")

    resp = client.get(f"/v1/jobs/{job_a.id}/result", headers={"X-API-Key": b_key})
    assert resp.status_code == 404


def test_get_result_missing_job_returns_404(client: TestClient, session: Session) -> None:
    api_key, _customer = _seed_customer(session)
    resp = client.get("/v1/jobs/missing123/result", headers={"X-API-Key": api_key})
    assert resp.status_code == 404


def test_preview_url_set_for_vllm_when_preview_generated(
    client: TestClient, session: Session, _patch_storage: LocalStorage
) -> None:
    """vLLM-served jobs should expose preview_url when the worker has
    written the Markdown→HTML preview to disk."""
    api_key, customer = _seed_customer(session)
    job = _seed_job(session, customer)
    job.engine = "vllm"
    session.add(job)
    session.commit()
    _patch_storage.save_result(job.id, b"# md", "md")
    # Simulate the worker writing the HTML preview
    _patch_storage.save_preview(job.id, b"<!doctype html><html></html>")

    resp = client.get(f"/v1/jobs/{job.id}", headers={"X-API-Key": api_key})

    assert resp.status_code == 200
    body = resp.json()
    assert body["preview_url"] == f"/v1/jobs/{job.id}/preview"
    assert body["engine"] == "vllm"
    assert body["structure_url"] is None  # still vllm-only constraint


def test_get_entities_returns_sidecar(
    client: TestClient, session: Session, _patch_storage: LocalStorage
) -> None:
    """The entities sidecar is served for any engine once present."""
    import json as jsonlib

    api_key, customer = _seed_customer(session)
    job = _seed_job(session, customer)
    job.engine = "vllm"  # engine-agnostic — vllm works too
    session.add(job)
    session.commit()
    sidecar = {"amounts": [{"raw": "500 EUR", "value": 500.0, "page": 1}], "meta": {}}
    _patch_storage.save_entities(job.id, jsonlib.dumps(sidecar).encode("utf-8"))

    resp = client.get(f"/v1/jobs/{job.id}/entities", headers={"X-API-Key": api_key})

    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("application/json")
    assert jsonlib.loads(resp.content)["amounts"][0]["value"] == 500.0


def test_job_detail_exposes_entities_url(
    client: TestClient, session: Session, _patch_storage: LocalStorage
) -> None:
    api_key, customer = _seed_customer(session)
    job = _seed_job(session, customer)
    session.add(job)
    session.commit()
    _patch_storage.save_result(job.id, b"# md", "md")
    _patch_storage.save_entities(job.id, b"{}")

    resp = client.get(f"/v1/jobs/{job.id}", headers={"X-API-Key": api_key})

    assert resp.json()["entities_url"] == f"/v1/jobs/{job.id}/entities"


def test_get_entities_404_when_absent(
    client: TestClient, session: Session, _patch_storage: LocalStorage
) -> None:
    api_key, customer = _seed_customer(session)
    job = _seed_job(session, customer)
    _patch_storage.save_result(job.id, b"# md", "md")

    resp = client.get(f"/v1/jobs/{job.id}/entities", headers={"X-API-Key": api_key})
    assert resp.status_code == 404
