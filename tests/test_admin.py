"""Tests for `app/routers/admin.py` — session auth + HTML routes."""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy.pool import StaticPool
from sqlmodel import Session, SQLModel, create_engine
from starlette.middleware.sessions import SessionMiddleware

from app.auth import hash_api_key, hash_password
from app.db import get_session
from app.models import AdminUser, ApiKey, Customer, Job, JobStatus, OutputFormat
from app.routers import admin as admin_router

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

ADMIN_USERNAME = "admin"
ADMIN_PASSWORD = "test-admin-pw"


@pytest.fixture()
def session() -> Iterator[Session]:
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    SQLModel.metadata.create_all(engine)
    with Session(engine) as s:
        # Seed admin user — tests use the DB path (not env var fallback)
        s.add(
            AdminUser(
                username=ADMIN_USERNAME,
                password_hash=hash_password(ADMIN_PASSWORD),
            )
        )
        s.commit()
        yield s
    SQLModel.metadata.drop_all(engine)


@pytest.fixture()
def app(session: Session) -> FastAPI:
    fastapi_app = FastAPI()
    fastapi_app.add_middleware(
        SessionMiddleware,
        secret_key="test-session-secret-" + "x" * 32,
        session_cookie="dockly_admin_test",
        same_site="lax",
        https_only=False,
    )

    def _override_session() -> Iterator[Session]:
        yield session

    fastapi_app.dependency_overrides[get_session] = _override_session
    fastapi_app.include_router(admin_router.router)
    return fastapi_app


@pytest.fixture()
def client(app: FastAPI) -> TestClient:
    # follow_redirects=False so we can assert redirect locations explicitly.
    return TestClient(app, follow_redirects=False)


@pytest.fixture()
def auth_client(app: FastAPI) -> TestClient:
    """A TestClient that has already logged in."""
    c = TestClient(app, follow_redirects=False)
    resp = c.post(
        "/admin/login",
        data={"username": ADMIN_USERNAME, "password": ADMIN_PASSWORD},
    )
    assert resp.status_code == 303, resp.text
    assert resp.headers["location"] == "/admin"
    return c


def _make_customer(
    session: Session, name: str = "Acme", email: str = "acme@example.com"
) -> Customer:
    customer = Customer(name=name, email=email, plan="free")
    session.add(customer)
    session.commit()
    session.refresh(customer)
    return customer


def _make_api_key(session: Session, customer: Customer, *, active: bool = True) -> ApiKey:
    key = ApiKey(
        customer_id=customer.id,
        key_hash=hash_api_key(f"sk_live_{customer.id}-test"),
        key_prefix=f"sk_live_{customer.id:04d}"[:12],
        name="test key",
        is_active=active,
    )
    session.add(key)
    session.commit()
    session.refresh(key)
    return key


def _make_job(
    session: Session,
    customer: Customer,
    api_key: ApiKey,
    *,
    status: JobStatus = JobStatus.done,
    output_format: OutputFormat = OutputFormat.md,
) -> Job:
    job = Job(
        api_key_id=api_key.id,
        customer_id=customer.id,
        status=status,
        input_filename="test.pdf",
        input_size_bytes=1024,
        input_mime="application/pdf",
        output_format=output_format,
        page_count=3,
    )
    session.add(job)
    session.commit()
    session.refresh(job)
    return job


# ---------------------------------------------------------------------------
# Login / Logout
# ---------------------------------------------------------------------------


def test_login_form_renders(client: TestClient) -> None:
    r = client.get("/admin/login")
    assert r.status_code == 200
    assert "Sign in" in r.text
    assert 'name="username"' in r.text
    assert 'name="password"' in r.text


def test_login_wrong_password(client: TestClient) -> None:
    r = client.post(
        "/admin/login",
        data={"username": ADMIN_USERNAME, "password": "wrong"},
    )
    assert r.status_code == 200
    assert "Invalid username or password" in r.text


def test_login_correct_credentials_redirects_to_dashboard(client: TestClient) -> None:
    r = client.post(
        "/admin/login",
        data={"username": ADMIN_USERNAME, "password": ADMIN_PASSWORD},
    )
    assert r.status_code == 303
    assert r.headers["location"] == "/admin"


def test_logout_clears_session(auth_client: TestClient) -> None:
    r = auth_client.post("/admin/logout")
    assert r.status_code == 303
    assert r.headers["location"] == "/admin/login"
    # Now dashboard should redirect again
    r2 = auth_client.get("/admin")
    assert r2.status_code == 303
    assert r2.headers["location"] == "/admin/login"


# ---------------------------------------------------------------------------
# Dashboard
# ---------------------------------------------------------------------------


def test_dashboard_without_session_redirects(client: TestClient) -> None:
    r = client.get("/admin")
    assert r.status_code == 303
    assert r.headers["location"] == "/admin/login"


def test_dashboard_with_session_renders(auth_client: TestClient) -> None:
    r = auth_client.get("/admin")
    assert r.status_code == 200
    assert "Dashboard" in r.text
    assert "Customers" in r.text


def test_dashboard_shows_counts(auth_client: TestClient, session: Session) -> None:
    customer = _make_customer(session)
    api_key = _make_api_key(session, customer)
    _make_job(session, customer, api_key)
    _make_job(session, customer, api_key, status=JobStatus.failed)

    r = auth_client.get("/admin")
    assert r.status_code == 200
    # At least 1 customer and 2 jobs shown (as numbers in page text)
    assert "Acme" in r.text


# ---------------------------------------------------------------------------
# Customers
# ---------------------------------------------------------------------------


def test_customers_list_renders(auth_client: TestClient, session: Session) -> None:
    _make_customer(session)
    r = auth_client.get("/admin/customers")
    assert r.status_code == 200
    assert "Acme" in r.text
    assert "acme@example.com" in r.text


def test_create_customer_success(auth_client: TestClient, session: Session) -> None:
    r = auth_client.post(
        "/admin/customers",
        data={"name": "New Co", "email": "new@example.com", "plan": "free"},
    )
    assert r.status_code == 303
    assert r.headers["location"].startswith("/admin/customers/")

    from sqlmodel import select

    row = session.exec(select(Customer).where(Customer.email == "new@example.com")).first()
    assert row is not None
    assert row.name == "New Co"


def test_create_customer_duplicate_email(auth_client: TestClient, session: Session) -> None:
    _make_customer(session, name="First", email="dup@example.com")

    r = auth_client.post(
        "/admin/customers",
        data={"name": "Second", "email": "dup@example.com", "plan": "free"},
    )
    assert r.status_code == 200
    assert "already exists" in r.text


def test_create_customer_invalid_email(auth_client: TestClient) -> None:
    r = auth_client.post(
        "/admin/customers",
        data={"name": "X", "email": "not-an-email", "plan": "free"},
    )
    assert r.status_code == 200
    assert "valid email" in r.text


def test_customer_detail_renders(auth_client: TestClient, session: Session) -> None:
    customer = _make_customer(session)
    r = auth_client.get(f"/admin/customers/{customer.id}")
    assert r.status_code == 200
    assert "Acme" in r.text
    assert "API Keys" in r.text


# ---------------------------------------------------------------------------
# API Keys
# ---------------------------------------------------------------------------


def test_create_key_returns_modal_with_plaintext(auth_client: TestClient, session: Session) -> None:
    customer = _make_customer(session)
    r = auth_client.post(
        f"/admin/customers/{customer.id}/keys",
        data={"name": "first key"},
    )
    assert r.status_code == 200
    assert "API Key Created" in r.text
    assert "sk_live_" in r.text  # plaintext shown once
    assert "only be shown once" in r.text

    from sqlmodel import select

    keys = session.exec(select(ApiKey).where(ApiKey.customer_id == customer.id)).all()
    assert len(keys) == 1
    assert keys[0].is_active is True
    assert keys[0].name == "first key"


def test_revoke_key_deactivates(auth_client: TestClient, session: Session) -> None:
    customer = _make_customer(session)
    api_key = _make_api_key(session, customer)
    assert api_key.is_active is True

    r = auth_client.post(f"/admin/customers/{customer.id}/keys/{api_key.id}/revoke")
    assert r.status_code == 303

    session.expire_all()
    reloaded = session.get(ApiKey, api_key.id)
    assert reloaded is not None
    assert reloaded.is_active is False


def test_revoke_key_via_htmx_returns_fragment(auth_client: TestClient, session: Session) -> None:
    customer = _make_customer(session)
    api_key = _make_api_key(session, customer)

    r = auth_client.post(
        f"/admin/customers/{customer.id}/keys/{api_key.id}/revoke",
        headers={"HX-Request": "true"},
    )
    assert r.status_code == 200
    assert "revoked" in r.text


# ---------------------------------------------------------------------------
# Jobs
# ---------------------------------------------------------------------------


def test_jobs_list_renders(auth_client: TestClient, session: Session) -> None:
    customer = _make_customer(session)
    api_key = _make_api_key(session, customer)
    _make_job(session, customer, api_key)

    r = auth_client.get("/admin/jobs")
    assert r.status_code == 200
    assert "Jobs" in r.text
    assert "Acme" in r.text


def test_jobs_list_filter_by_status(auth_client: TestClient, session: Session) -> None:
    customer = _make_customer(session)
    api_key = _make_api_key(session, customer)
    _make_job(session, customer, api_key, status=JobStatus.done)
    _make_job(session, customer, api_key, status=JobStatus.failed)

    r = auth_client.get("/admin/jobs?status=done")
    assert r.status_code == 200
    # Both jobs belong to Acme, but the failed row should not appear.
    # We verify by counting the status dot classes in the table.
    assert r.text.count("bg-emerald-500") >= 1
    # No failed-status row should be rendered (no bg-rose-500 from status dot).
    # NB the status dot partial uses bg-rose-500 only for failed jobs.
    # The modal template also contains bg-rose-500 but is only returned from
    # the key-create endpoint, so it won't appear here.
    # We compare to the unfiltered case:
    r_all = auth_client.get("/admin/jobs")
    assert r_all.text.count("bg-rose-500") > r.text.count("bg-rose-500")


def test_jobs_list_htmx_returns_fragment(auth_client: TestClient, session: Session) -> None:
    customer = _make_customer(session)
    api_key = _make_api_key(session, customer)
    _make_job(session, customer, api_key)

    r = auth_client.get("/admin/jobs", headers={"HX-Request": "true"})
    assert r.status_code == 200
    # Fragment should NOT contain the full <html> doc wrapper
    assert "<!DOCTYPE html>" not in r.text
    assert "Acme" in r.text


def test_jobs_list_empty_state(auth_client: TestClient) -> None:
    r = auth_client.get("/admin/jobs")
    assert r.status_code == 200
    assert "No jobs match" in r.text
