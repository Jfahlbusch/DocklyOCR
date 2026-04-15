"""CLI: print a bcrypt hash of a plaintext password.

Usage:
    .venv/bin/python scripts/hash_password.py '<plaintext>'
    .venv/bin/python scripts/hash_password.py            # interactive (no echo)
"""

from __future__ import annotations

import getpass
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from app.auth import hash_password  # noqa: E402


def main() -> int:
    if len(sys.argv) > 2:
        print("Usage: hash_password.py [<plaintext>]", file=sys.stderr)
        return 2

    if len(sys.argv) == 2:
        plaintext = sys.argv[1]
    else:
        plaintext = getpass.getpass("Password: ")
        confirm = getpass.getpass("Confirm:  ")
        if plaintext != confirm:
            print("ERROR: passwords do not match", file=sys.stderr)
            return 1

    if not plaintext:
        print("ERROR: empty password", file=sys.stderr)
        return 1

    print(hash_password(plaintext))
    return 0


if __name__ == "__main__":
    sys.exit(main())
