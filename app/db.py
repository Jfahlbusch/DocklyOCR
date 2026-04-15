"""SQLModel engine + session helpers for DocklyOCR.

For SQLite database URLs we ensure the parent directory exists before the
engine is created (the production container mounts `/data` empty).
"""

from __future__ import annotations

import contextlib
from collections.abc import Iterator
from pathlib import Path
from urllib.parse import urlparse

from sqlmodel import Session, SQLModel, create_engine

from app.config import settings


def _ensure_sqlite_parent_dir(database_url: str) -> None:
    """For SQLite URLs, create the parent directory of the DB file if missing.

    Best-effort: silently skips if the path is unwritable (e.g. running tests
    on macOS with the default container path `/data`). The actual engine
    connection will surface a clearer error at use-time if the DB really
    can't be written.
    """
    if not database_url.startswith("sqlite"):
        return
    # SQLAlchemy SQLite URLs look like:
    #   sqlite:///relative/path.db        → 3 slashes → relative
    #   sqlite:////data/ocr.db            → 4 slashes → absolute
    #   sqlite:///:memory:                → in-memory, skip
    parsed = urlparse(database_url)
    db_path = parsed.path
    if not db_path or db_path == "/:memory:" or ":memory:" in db_path:
        return
    parent = Path(db_path).parent
    if not str(parent) or parent == Path("."):
        return
    # Best-effort: read-only FS or missing perms is ignored at import time.
    with contextlib.suppress(OSError):
        parent.mkdir(parents=True, exist_ok=True)


_connect_args: dict = {}
if settings.database_url.startswith("sqlite"):
    _connect_args["check_same_thread"] = False
    _ensure_sqlite_parent_dir(settings.database_url)


engine = create_engine(
    settings.database_url,
    connect_args=_connect_args,
    echo=False,
)


def init_db() -> None:
    """Create all tables (idempotent)."""
    # Importing models here ensures their metadata is registered with SQLModel
    # before `create_all` runs, even if `db.py` is imported before `models.py`.
    from app import models  # noqa: F401

    SQLModel.metadata.create_all(engine)


def get_session() -> Iterator[Session]:
    """FastAPI dependency that yields a SQLModel session."""
    with Session(engine) as session:
        yield session
