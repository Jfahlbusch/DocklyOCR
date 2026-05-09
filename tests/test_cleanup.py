"""Tests for the TTL cleanup of old jobs and storage folders."""

from __future__ import annotations

from datetime import datetime, timedelta
from pathlib import Path

import pytest
from sqlmodel import Session, SQLModel, create_engine

from app.models import ApiKey, Customer, Job, JobStatus, OutputFormat
from app.services import cleanup as cleanup_module


@pytest.fixture
def temp_storage(tmp_path, monkeypatch):
    """Storage dir + DB engine pointed at the temp tree."""
    storage = tmp_path / "storage"
    storage.mkdir()
    db_path = tmp_path / "test.db"
    engine = create_engine(f"sqlite:///{db_path}", connect_args={"check_same_thread": False})
    SQLModel.metadata.create_all(engine)

    monkeypatch.setattr(cleanup_module.settings, "storage_dir", str(storage))
    monkeypatch.setattr(cleanup_module.settings, "result_ttl_days", 7)
    monkeypatch.setattr(cleanup_module, "engine", engine)
    return storage, engine


def _seed_job(
    engine,
    storage: Path,
    *,
    job_id: str,
    age_days: float,
    status: JobStatus,
    payload_size: int = 1024,
) -> None:
    """Insert a Job row + a fake artefact file in the storage folder."""
    with Session(engine) as s:
        c = Customer(name="t", email=f"t{job_id}@x", created_at=datetime.utcnow())
        s.add(c)
        s.commit()
        s.refresh(c)
        ak = ApiKey(
            customer_id=c.id,
            key_hash=f"h{job_id}",
            key_prefix="sk_test",
            name="t",
            created_at=datetime.utcnow(),
        )
        s.add(ak)
        s.commit()
        s.refresh(ak)
        job = Job(
            id=job_id,
            api_key_id=ak.id,
            customer_id=c.id,
            status=status,
            input_filename=f"{job_id}.pdf",
            input_size_bytes=payload_size,
            input_mime="application/pdf",
            output_format=OutputFormat.md,
            created_at=datetime.utcnow() - timedelta(days=age_days),
        )
        s.add(job)
        s.commit()

    job_dir = storage / job_id
    job_dir.mkdir()
    (job_dir / "input.pdf").write_bytes(b"x" * payload_size)


def test_cleanup_deletes_old_done_jobs(temp_storage):
    storage, engine = temp_storage
    _seed_job(engine, storage, job_id="old1", age_days=10, status=JobStatus.done)

    report = cleanup_module.cleanup_old_jobs(dry_run=False)

    assert report.deleted_count == 1
    assert report.deleted_bytes >= 1024
    assert not (storage / "old1").exists()
    with Session(engine) as s:
        assert s.get(Job, "old1") is None


def test_cleanup_keeps_recent_jobs(temp_storage):
    storage, engine = temp_storage
    _seed_job(engine, storage, job_id="recent", age_days=2, status=JobStatus.done)

    report = cleanup_module.cleanup_old_jobs(dry_run=False)

    assert report.deleted_count == 0
    assert (storage / "recent").exists()
    with Session(engine) as s:
        assert s.get(Job, "recent") is not None


def test_cleanup_skips_in_flight_jobs(temp_storage):
    """Never delete pending/processing jobs even if old (defensive)."""
    storage, engine = temp_storage
    _seed_job(engine, storage, job_id="stuck", age_days=99, status=JobStatus.processing)

    report = cleanup_module.cleanup_old_jobs(dry_run=False)

    assert report.deleted_count == 0
    assert (storage / "stuck").exists()


def test_cleanup_dry_run_changes_nothing(temp_storage):
    storage, engine = temp_storage
    _seed_job(engine, storage, job_id="old2", age_days=10, status=JobStatus.failed)

    report = cleanup_module.cleanup_old_jobs(dry_run=True)

    assert report.eligible_count == 1
    assert report.deleted_count == 0
    assert (storage / "old2").exists()
    with Session(engine) as s:
        assert s.get(Job, "old2") is not None


def test_cleanup_ttl_override(temp_storage):
    storage, engine = temp_storage
    _seed_job(engine, storage, job_id="medium", age_days=5, status=JobStatus.done)

    # Default TTL is 7 → not eligible
    r1 = cleanup_module.cleanup_old_jobs(dry_run=True)
    assert r1.eligible_count == 0

    # Override to 3 → now eligible
    r2 = cleanup_module.cleanup_old_jobs(ttl_days=3, dry_run=True)
    assert r2.eligible_count == 1


def test_cleanup_handles_missing_storage_folder(temp_storage):
    """Job exists in DB but its storage dir was already deleted manually."""
    storage, engine = temp_storage
    _seed_job(engine, storage, job_id="orphan", age_days=10, status=JobStatus.done)
    import shutil

    shutil.rmtree(storage / "orphan")

    report = cleanup_module.cleanup_old_jobs(dry_run=False)

    assert report.deleted_count == 1  # DB row still gets removed
    with Session(engine) as s:
        assert s.get(Job, "orphan") is None
