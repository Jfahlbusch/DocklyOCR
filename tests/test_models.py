"""Tests for `app/models.py` — SQLModel table definitions."""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from sqlalchemy.pool import StaticPool
from sqlmodel import Session, SQLModel, create_engine, select

from app.models import (
    AdminUser,
    ApiKey,
    Customer,
    Job,
    JobStatus,
    OutputFormat,
)


@pytest.fixture()
def session() -> Iterator[Session]:
    """In-memory SQLite session, fresh per test."""
    engine = create_engine(
        "sqlite://",  # in-memory
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    SQLModel.metadata.create_all(engine)
    with Session(engine) as s:
        yield s
    SQLModel.metadata.drop_all(engine)


def test_create_customer_defaults(session: Session) -> None:
    customer = Customer(name="Acme GmbH", email="ops@acme.example")
    session.add(customer)
    session.commit()
    session.refresh(customer)

    assert customer.id is not None
    assert customer.plan == "free"
    assert customer.is_active is True
    assert customer.monthly_page_limit is None
    assert customer.webhook_secret is None
    assert customer.created_at is not None


def test_create_api_key_linked_to_customer(session: Session) -> None:
    customer = Customer(name="Acme", email="ops@acme.example")
    session.add(customer)
    session.commit()
    session.refresh(customer)

    key = ApiKey(
        customer_id=customer.id,  # type: ignore[arg-type]
        key_hash="a" * 64,
        key_prefix="sk_live_abcd",
        name="Production",
    )
    session.add(key)
    session.commit()
    session.refresh(key)

    assert key.id is not None
    assert key.key_hash == "a" * 64
    assert key.key_prefix == "sk_live_abcd"
    assert key.is_active is True
    assert key.last_used_at is None

    # Relationship round-trip
    fetched = session.exec(select(Customer).where(Customer.id == customer.id)).one()
    assert len(fetched.api_keys) == 1
    assert fetched.api_keys[0].name == "Production"


def test_create_job_defaults_and_uuid(session: Session) -> None:
    customer = Customer(name="Acme", email="ops@acme.example")
    session.add(customer)
    session.commit()
    session.refresh(customer)
    key = ApiKey(
        customer_id=customer.id,  # type: ignore[arg-type]
        key_hash="b" * 64,
        key_prefix="sk_live_efgh",
        name="Test",
    )
    session.add(key)
    session.commit()
    session.refresh(key)

    job = Job(
        api_key_id=key.id,  # type: ignore[arg-type]
        customer_id=customer.id,  # type: ignore[arg-type]
        input_filename="invoice.pdf",
        input_size_bytes=12345,
        input_mime="application/pdf",
        output_format=OutputFormat.md,
    )
    session.add(job)
    session.commit()
    session.refresh(job)

    # UUID4 hex is 32 chars, all lowercase hex
    assert isinstance(job.id, str)
    assert len(job.id) == 32
    assert all(c in "0123456789abcdef" for c in job.id)

    assert job.status == JobStatus.pending
    assert job.webhook_delivered is False
    assert job.webhook_attempts == 0
    assert job.page_count is None
    assert job.created_at is not None


def test_job_status_enum_round_trip(session: Session) -> None:
    customer = Customer(name="Acme", email="ops@acme.example")
    session.add(customer)
    session.commit()
    session.refresh(customer)
    key = ApiKey(
        customer_id=customer.id,  # type: ignore[arg-type]
        key_hash="c" * 64,
        key_prefix="sk_live_ijkl",
        name="K",
    )
    session.add(key)
    session.commit()
    session.refresh(key)

    job = Job(
        api_key_id=key.id,  # type: ignore[arg-type]
        customer_id=customer.id,  # type: ignore[arg-type]
        input_filename="x.pdf",
        input_size_bytes=1,
        input_mime="application/pdf",
        output_format=OutputFormat.json,
    )
    session.add(job)
    session.commit()
    session.refresh(job)

    job.status = JobStatus.done
    session.add(job)
    session.commit()

    fetched = session.exec(select(Job).where(Job.id == job.id)).one()
    assert fetched.status == JobStatus.done
    assert fetched.status.value == "done"
    assert fetched.output_format == OutputFormat.json


def test_admin_user_unique_username(session: Session) -> None:
    admin = AdminUser(username="admin", password_hash="bcrypt$dummy")
    session.add(admin)
    session.commit()

    fetched = session.exec(select(AdminUser).where(AdminUser.username == "admin")).one()
    assert fetched.id is not None
    assert fetched.password_hash == "bcrypt$dummy"
