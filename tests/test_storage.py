"""Tests for `app/services/storage.py`."""

from __future__ import annotations

from pathlib import Path

import pytest

from app.services.storage import LocalStorage, _sanitize_filename


@pytest.fixture()
def storage(tmp_path: Path) -> LocalStorage:
    return LocalStorage(tmp_path / "storage")


def test_save_upload_returns_expected_path(storage: LocalStorage) -> None:
    path = storage.save_upload("job_abc123", "sample.pdf", b"%PDF-1.4\n...")
    assert path.exists()
    assert path.read_bytes() == b"%PDF-1.4\n..."
    assert path.parent.name == "job_abc123"
    assert path.name == "input_sample.pdf"


def test_save_result_returns_expected_path(storage: LocalStorage) -> None:
    path = storage.save_result("job_xyz", b"# hello", "md")
    assert path.exists()
    assert path.read_bytes() == b"# hello"
    assert path.name == "result.md"


def test_get_input_path_returns_saved_upload(storage: LocalStorage) -> None:
    storage.save_upload("j1", "doc.pdf", b"x")
    got = storage.get_input_path("j1")
    assert got is not None
    assert got.name == "input_doc.pdf"


def test_get_input_path_missing_job(storage: LocalStorage) -> None:
    assert storage.get_input_path("does_not_exist") is None


def test_get_result_path_returns_saved_result(storage: LocalStorage) -> None:
    storage.save_result("j2", b"{}", "json")
    got = storage.get_result_path("j2")
    assert got is not None
    assert got.name == "result.json"


def test_get_result_path_missing_job(storage: LocalStorage) -> None:
    assert storage.get_result_path("does_not_exist") is None


def test_delete_job_removes_directory(storage: LocalStorage) -> None:
    storage.save_upload("j3", "a.pdf", b"x")
    storage.save_result("j3", b"y", "txt")
    assert (storage.base_dir / "j3").exists()
    storage.delete_job("j3")
    assert not (storage.base_dir / "j3").exists()


def test_delete_job_silently_ignores_missing(storage: LocalStorage) -> None:
    # Should not raise
    storage.delete_job("never_existed")


# --- _sanitize_filename -----------------------------------------------------


def test_sanitize_strips_path_traversal() -> None:
    assert _sanitize_filename("../../etc/passwd") == "passwd"
    assert _sanitize_filename("/absolute/path/file.pdf") == "file.pdf"
    assert _sanitize_filename("..\\windows\\system32\\bad.pdf").endswith("bad.pdf")


def test_sanitize_replaces_special_chars() -> None:
    assert _sanitize_filename("hello world.pdf") == "hello_world.pdf"
    assert _sanitize_filename("file;rm -rf.pdf") == "file_rm_-rf.pdf"
    assert _sanitize_filename("ümlaut.pdf").endswith(".pdf")
    assert "ü" not in _sanitize_filename("ümlaut.pdf")


def test_sanitize_preserves_allowed_chars() -> None:
    assert _sanitize_filename("my-file_2024.PDF") == "my-file_2024.PDF"


def test_sanitize_truncates_to_120() -> None:
    name = "a" * 200 + ".pdf"
    sanitized = _sanitize_filename(name)
    assert len(sanitized) == 120


def test_sanitize_empty_returns_upload() -> None:
    assert _sanitize_filename("") == "upload"
    assert _sanitize_filename("////") == "upload"


def test_save_upload_with_malicious_filename(storage: LocalStorage) -> None:
    """Path-traversal attempt must not escape the job directory."""
    path = storage.save_upload("safe_job", "../../etc/passwd", b"hacked")
    assert path.exists()
    # Normalised parent is the job dir — not escaped up
    assert path.parent == storage.base_dir / "safe_job"
    assert path.name == "input_passwd"
