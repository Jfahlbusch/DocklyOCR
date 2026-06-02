"""Local filesystem storage with an S3-ready interface.

All job artifacts (original upload + formatted result) live under
``{storage_dir}/{job_id}/``. Filenames are sanitized to avoid path-traversal
and filesystem-hostile characters, and capped at 120 chars.
"""

from __future__ import annotations

import contextlib
import re
import shutil
from os.path import basename
from pathlib import Path

from app.config import settings


class LocalStorage:
    """Local filesystem-backed blob store for job artifacts.

    Interface is intentionally S3-shaped — the same method set can be reimplemented
    against ``boto3`` without touching the callers.
    """

    def __init__(self, base_dir: Path) -> None:
        self.base_dir = Path(base_dir)
        # Best-effort: the production container mounts ``/data`` writable but
        # tests on macOS may hit permission denied on the default config path.
        # Callers that actually need writes will surface a clearer error later.
        with contextlib.suppress(OSError):
            self.base_dir.mkdir(parents=True, exist_ok=True)

    def save_upload(self, job_id: str, filename: str, data: bytes) -> Path:
        """Store the original upload. Returns the absolute path."""
        job_dir = self.base_dir / job_id
        job_dir.mkdir(parents=True, exist_ok=True)
        safe_name = _sanitize_filename(filename)
        path = job_dir / f"input_{safe_name}"
        path.write_bytes(data)
        return path

    def save_result(self, job_id: str, body: bytes, extension: str) -> Path:
        """Store the formatted result. Returns the absolute path."""
        job_dir = self.base_dir / job_id
        job_dir.mkdir(parents=True, exist_ok=True)
        path = job_dir / f"result.{extension}"
        path.write_bytes(body)
        return path

    def get_input_path(self, job_id: str) -> Path | None:
        job_dir = self.base_dir / job_id
        if not job_dir.exists():
            return None
        for p in job_dir.iterdir():
            if p.name.startswith("input_"):
                return p
        return None

    def get_result_path(self, job_id: str) -> Path | None:
        job_dir = self.base_dir / job_id
        if not job_dir.exists():
            return None
        for p in job_dir.iterdir():
            if p.name.startswith("result."):
                return p
        return None

    def save_structure(self, job_id: str, body: bytes) -> Path:
        """Store the opendataloader structured JSON sidecar.

        The structure file carries per-element bounding boxes, page
        numbers, heading levels, and tables — everything that the plain
        markdown body throws away. Only present for ``engine=opendataloader``
        jobs.
        """
        job_dir = self.base_dir / job_id
        job_dir.mkdir(parents=True, exist_ok=True)
        path = job_dir / "structure.json"
        path.write_bytes(body)
        return path

    def get_structure_path(self, job_id: str) -> Path | None:
        job_dir = self.base_dir / job_id
        if not job_dir.exists():
            return None
        p = job_dir / "structure.json"
        return p if p.exists() else None

    def delete_job(self, job_id: str) -> None:
        job_dir = self.base_dir / job_id
        if job_dir.exists():
            shutil.rmtree(job_dir)


_SANITIZE_RE = re.compile(r"[^A-Za-z0-9._-]")


def _sanitize_filename(name: str) -> str:
    """Strip path components, keep alnum/dot/dash/underscore, cap length at 120.

    Returns ``"upload"`` if the sanitized name would be empty.
    """
    base = basename(name or "")
    cleaned = _SANITIZE_RE.sub("_", base)
    cleaned = cleaned[:120]
    return cleaned or "upload"


storage = LocalStorage(Path(settings.storage_dir))
