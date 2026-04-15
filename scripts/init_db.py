"""CLI: initialize the DocklyOCR database and seed the admin user.

Usage:
    .venv/bin/python scripts/init_db.py
"""

from __future__ import annotations

import sys
from pathlib import Path

# Allow `python scripts/init_db.py` from project root
PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from sqlmodel import Session, select  # noqa: E402

from app.config import settings  # noqa: E402
from app.db import engine, init_db  # noqa: E402
from app.models import AdminUser  # noqa: E402


def main() -> int:
    print(f"DocklyOCR DB init — database_url={settings.database_url}")

    init_db()
    print("  - tables created (or already existed)")

    if not settings.admin_password_hash:
        print(
            "ERROR: ADMIN_PASSWORD_HASH is empty.\n"
            "       Generate one with:\n"
            "         .venv/bin/python scripts/hash_password.py '<your-password>'\n"
            "       Then set ADMIN_PASSWORD_HASH in .env and rerun this script.",
            file=sys.stderr,
        )
        return 1

    with Session(engine) as session:
        existing = session.exec(
            select(AdminUser).where(AdminUser.username == settings.admin_username)
        ).first()
        if existing:
            print(f"  - admin user '{settings.admin_username}' already exists — skipping")
        else:
            admin = AdminUser(
                username=settings.admin_username,
                password_hash=settings.admin_password_hash,
            )
            session.add(admin)
            session.commit()
            print(f"  - admin user '{settings.admin_username}' created")

    print("Done.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
