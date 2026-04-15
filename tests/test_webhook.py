"""Tests for `app/services/webhook.py`."""

from __future__ import annotations

import hashlib
import hmac
import json
from collections.abc import Iterator

import httpx
import pytest
from sqlalchemy.pool import StaticPool
from sqlmodel import Session, SQLModel, create_engine

from app.models import ApiKey, Customer, Job, JobStatus, OutputFormat
from app.services import webhook as webhook_module


@pytest.fixture()
def engine_fixture(monkeypatch: pytest.MonkeyPatch):
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    SQLModel.metadata.create_all(engine)
    monkeypatch.setattr(webhook_module, "engine", engine)
    yield engine
    SQLModel.metadata.drop_all(engine)


@pytest.fixture()
def session(engine_fixture) -> Iterator[Session]:
    with Session(engine_fixture) as s:
        yield s


def _seed_job_with_customer(
    session: Session,
    *,
    webhook_url: str = "https://example.test/hook",
    webhook_secret: str | None = None,
) -> Job:
    customer = Customer(
        name="Acme",
        email=f"test-{id(session)}-{webhook_secret}@example.com",
        webhook_secret=webhook_secret,
    )
    session.add(customer)
    session.commit()
    session.refresh(customer)

    key = ApiKey(
        customer_id=customer.id,  # type: ignore[arg-type]
        key_hash="deadbeef" * 8,
        key_prefix="sk_live_aaaa",
        name="test",
    )
    session.add(key)
    session.commit()
    session.refresh(key)

    job = Job(
        api_key_id=key.id,  # type: ignore[arg-type]
        customer_id=customer.id,  # type: ignore[arg-type]
        status=JobStatus.done,
        input_filename="sample.pdf",
        input_size_bytes=1024,
        input_mime="application/pdf",
        output_format=OutputFormat.md,
        webhook_url=webhook_url,
        page_count=2,
        pages_ok=2,
        pages_failed=0,
    )
    session.add(job)
    session.commit()
    session.refresh(job)
    return job


def _install_transport(
    monkeypatch: pytest.MonkeyPatch,
    handler,
) -> dict:
    """Swap the webhook client factory so it uses a MockTransport."""
    captured: dict = {}

    def _capture_handler(request: httpx.Request) -> httpx.Response:
        captured["request"] = request
        captured["headers"] = dict(request.headers)
        captured["body"] = request.content
        return handler(request)

    transport = httpx.MockTransport(_capture_handler)

    def _factory() -> httpx.AsyncClient:
        return httpx.AsyncClient(transport=transport, timeout=5.0)

    monkeypatch.setattr(webhook_module, "_client_factory", _factory)
    return captured


@pytest.mark.asyncio
async def test_success_marks_delivered(
    engine_fixture, session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    job = _seed_job_with_customer(session)

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"ok": True})

    _install_transport(monkeypatch, handler)

    result = await webhook_module.deliver_webhook(job.id)
    assert result is True

    session.expire_all()
    refreshed = session.get(Job, job.id)
    assert refreshed is not None
    assert refreshed.webhook_delivered is True
    assert refreshed.webhook_attempts == 1


@pytest.mark.asyncio
async def test_500_marks_failure_and_attempts(
    engine_fixture, session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    job = _seed_job_with_customer(session)

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, text="oops")

    _install_transport(monkeypatch, handler)

    result = await webhook_module.deliver_webhook(job.id)
    assert result is False

    session.expire_all()
    refreshed = session.get(Job, job.id)
    assert refreshed is not None
    assert refreshed.webhook_delivered is False
    assert refreshed.webhook_attempts == 1


@pytest.mark.asyncio
async def test_transport_error_marks_failure(
    engine_fixture, session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    job = _seed_job_with_customer(session)

    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectTimeout("simulated timeout", request=request)

    _install_transport(monkeypatch, handler)

    result = await webhook_module.deliver_webhook(job.id)
    assert result is False

    session.expire_all()
    refreshed = session.get(Job, job.id)
    assert refreshed is not None
    assert refreshed.webhook_delivered is False
    assert refreshed.webhook_attempts == 1


@pytest.mark.asyncio
async def test_signature_header_with_secret(
    engine_fixture, session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    secret = "top-secret-webhook-key"
    job = _seed_job_with_customer(session, webhook_secret=secret)

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"ok": True})

    captured = _install_transport(monkeypatch, handler)

    result = await webhook_module.deliver_webhook(job.id)
    assert result is True

    assert "x-signature" in captured["headers"]
    sig_header = captured["headers"]["x-signature"]
    assert sig_header.startswith("sha256=")

    # Verify HMAC matches the exact sent body
    expected = hmac.new(secret.encode(), captured["body"], hashlib.sha256).hexdigest()
    assert sig_header == f"sha256={expected}"

    # Sanity check payload structure
    payload = json.loads(captured["body"])
    assert payload["job_id"] == job.id
    assert payload["status"] == "done"
    assert payload["output_format"] == "md"
    assert payload["page_count"] == 2
    assert payload["result_url"] == f"/v1/jobs/{job.id}/result"


@pytest.mark.asyncio
async def test_no_signature_header_without_secret(
    engine_fixture, session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    job = _seed_job_with_customer(session, webhook_secret=None)

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"ok": True})

    captured = _install_transport(monkeypatch, handler)

    await webhook_module.deliver_webhook(job.id)

    assert "x-signature" not in captured["headers"]


@pytest.mark.asyncio
async def test_user_agent_header(
    engine_fixture, session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    job = _seed_job_with_customer(session)

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200)

    captured = _install_transport(monkeypatch, handler)
    await webhook_module.deliver_webhook(job.id)

    assert captured["headers"]["user-agent"] == "ocr-api-webhook/1.0"
    assert captured["headers"]["content-type"] == "application/json"


@pytest.mark.asyncio
async def test_no_webhook_url_returns_false(
    engine_fixture, session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    job = _seed_job_with_customer(session, webhook_url="")
    # set explicitly None on the DB row
    db_job = session.get(Job, job.id)
    assert db_job is not None
    db_job.webhook_url = None
    session.add(db_job)
    session.commit()

    result = await webhook_module.deliver_webhook(job.id)
    assert result is False
