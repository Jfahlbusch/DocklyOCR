"""Tests for `app/auth.py` — API key + password + rate limiting."""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from fastapi import Depends, FastAPI, HTTPException
from fastapi.testclient import TestClient
from sqlalchemy.pool import StaticPool
from sqlmodel import Session, SQLModel, create_engine

from app import auth as auth_module
from app.auth import (
    ApiKeyContext,
    InMemoryRateLimiter,
    generate_api_key,
    hash_api_key,
    hash_password,
    require_api_key,
    verify_password,
)
from app.db import get_session
from app.models import ApiKey, Customer

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def session() -> Iterator[Session]:
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    SQLModel.metadata.create_all(engine)
    with Session(engine) as s:
        yield s
    SQLModel.metadata.drop_all(engine)


@pytest.fixture()
def app(session: Session) -> FastAPI:
    """Minimal FastAPI app with a `/test` route guarded by `require_api_key`."""
    fastapi_app = FastAPI()

    def _override_session() -> Iterator[Session]:
        yield session

    fastapi_app.dependency_overrides[get_session] = _override_session

    @fastapi_app.get("/test")
    async def _test_route(
        ctx: ApiKeyContext = Depends(require_api_key),  # noqa: B008
    ) -> dict:
        return {
            "customer_id": ctx.customer.id,
            "api_key_id": ctx.api_key.id,
            "customer_name": ctx.customer.name,
        }

    return fastapi_app


@pytest.fixture()
def client(app: FastAPI) -> TestClient:
    return TestClient(app)


def _seed_customer_with_key(
    session: Session,
    *,
    customer_active: bool = True,
    key_active: bool = True,
) -> tuple[str, Customer, ApiKey]:
    """Insert a customer + api key, return (plaintext, customer, api_key)."""
    customer = Customer(
        name="Acme",
        email=f"acme-{id(session)}-{customer_active}-{key_active}@example.com",
        is_active=customer_active,
    )
    session.add(customer)
    session.commit()
    session.refresh(customer)

    plaintext, key_hash, prefix = generate_api_key()
    key = ApiKey(
        customer_id=customer.id,  # type: ignore[arg-type]
        key_hash=key_hash,
        key_prefix=prefix,
        name="Production",
        is_active=key_active,
    )
    session.add(key)
    session.commit()
    session.refresh(key)
    return plaintext, customer, key


# ---------------------------------------------------------------------------
# generate_api_key / hash_api_key
# ---------------------------------------------------------------------------


def test_generate_api_key_format() -> None:
    plaintext, key_hash, prefix = generate_api_key()

    assert plaintext.startswith("sk_live_")
    assert len(key_hash) == 64
    assert all(c in "0123456789abcdef" for c in key_hash)
    assert len(prefix) == 12
    assert prefix == plaintext[:12]


def test_hash_api_key_deterministic() -> None:
    plaintext = "sk_live_DETERMINISTIC_TEST"
    assert hash_api_key(plaintext) == hash_api_key(plaintext)
    assert hash_api_key(plaintext) != hash_api_key(plaintext + "x")


def test_generate_api_key_is_unique() -> None:
    samples = {generate_api_key()[0] for _ in range(50)}
    assert len(samples) == 50  # essentially zero collision probability


# ---------------------------------------------------------------------------
# Password hashing
# ---------------------------------------------------------------------------


def test_hash_password_roundtrip() -> None:
    pw = "correct horse battery staple"
    hashed = hash_password(pw)
    assert hashed != pw
    assert verify_password(pw, hashed) is True
    assert verify_password("wrong password", hashed) is False


def test_verify_password_handles_garbage_hash() -> None:
    assert verify_password("anything", "not-a-bcrypt-hash") is False


# ---------------------------------------------------------------------------
# require_api_key dependency
# ---------------------------------------------------------------------------


def test_require_api_key_valid(client: TestClient, session: Session) -> None:
    plaintext, customer, key = _seed_customer_with_key(session)

    resp = client.get("/test", headers={"X-API-Key": plaintext})

    assert resp.status_code == 200
    body = resp.json()
    assert body["customer_id"] == customer.id
    assert body["api_key_id"] == key.id
    assert body["customer_name"] == "Acme"


def test_require_api_key_missing_header(client: TestClient) -> None:
    resp = client.get("/test")
    # FastAPI Header(...) returns 422 on missing required headers by default.
    # The contract is "anything other than 200" — verify we don't grant access.
    assert resp.status_code in (401, 422)


def test_require_api_key_wrong_key(client: TestClient, session: Session) -> None:
    _seed_customer_with_key(session)
    resp = client.get("/test", headers={"X-API-Key": "sk_live_does_not_exist"})
    assert resp.status_code == 401
    assert "Invalid" in resp.json()["detail"]


def test_require_api_key_inactive_key(client: TestClient, session: Session) -> None:
    plaintext, _, _ = _seed_customer_with_key(session, key_active=False)
    resp = client.get("/test", headers={"X-API-Key": plaintext})
    assert resp.status_code == 401


def test_require_api_key_inactive_customer(client: TestClient, session: Session) -> None:
    plaintext, _, _ = _seed_customer_with_key(session, customer_active=False)
    resp = client.get("/test", headers={"X-API-Key": plaintext})
    assert resp.status_code == 401


def test_require_api_key_updates_last_used_at(client: TestClient, session: Session) -> None:
    plaintext, _, key = _seed_customer_with_key(session)
    assert key.last_used_at is None

    resp = client.get("/test", headers={"X-API-Key": plaintext})
    assert resp.status_code == 200

    # Re-fetch from DB
    session.expire_all()
    refreshed = session.get(ApiKey, key.id)
    assert refreshed is not None
    assert refreshed.last_used_at is not None


# ---------------------------------------------------------------------------
# InMemoryRateLimiter
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_rate_limiter_allows_up_to_limit() -> None:
    limiter = InMemoryRateLimiter(requests_per_minute=10)
    for _ in range(10):
        info = await limiter.check(key_id=1)
        assert info.limit == 10
    # 11th request must raise
    with pytest.raises(HTTPException) as excinfo:
        await limiter.check(key_id=1)
    assert excinfo.value.status_code == 429
    assert "Rate limit" in excinfo.value.detail


@pytest.mark.asyncio
async def test_rate_limiter_isolated_per_key() -> None:
    limiter = InMemoryRateLimiter(requests_per_minute=2)
    await limiter.check(key_id=1)
    await limiter.check(key_id=1)
    # Key 2 has its own bucket
    await limiter.check(key_id=2)
    await limiter.check(key_id=2)

    with pytest.raises(HTTPException):
        await limiter.check(key_id=1)
    with pytest.raises(HTTPException):
        await limiter.check(key_id=2)


@pytest.mark.asyncio
async def test_rate_limiter_window_resets_after_60s(monkeypatch: pytest.MonkeyPatch) -> None:
    limiter = InMemoryRateLimiter(requests_per_minute=3)

    fake_now = {"t": 1_000_000.0}

    def fake_time() -> float:
        return fake_now["t"]

    monkeypatch.setattr(auth_module.time, "time", fake_time)

    # Fill the bucket
    await limiter.check(key_id=42)
    await limiter.check(key_id=42)
    await limiter.check(key_id=42)
    with pytest.raises(HTTPException):
        await limiter.check(key_id=42)

    # Advance time past the window
    fake_now["t"] += 61.0

    # Bucket should now be empty — should succeed again
    info = await limiter.check(key_id=42)
    assert info.remaining == 2  # one used out of three


@pytest.mark.asyncio
async def test_rate_limiter_remaining_decrements() -> None:
    limiter = InMemoryRateLimiter(requests_per_minute=5)
    info1 = await limiter.check(key_id=7)
    info2 = await limiter.check(key_id=7)
    info3 = await limiter.check(key_id=7)
    assert info1.remaining == 4
    assert info2.remaining == 3
    assert info3.remaining == 2
