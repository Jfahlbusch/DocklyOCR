"""Authentication and authorization primitives for DocklyOCR.

Provides:
- API-Key generation (`sk_live_<token>`) and SHA-256 hashing
- bcrypt password hashing (admin user)
- FastAPI dependency `require_api_key` that validates the `X-API-Key` header
- In-memory per-key rate limiter (token bucket / sliding window)
- Chainable dependency `enforce_rate_limit`
"""

from __future__ import annotations

import asyncio
import hashlib
import secrets
import time
from collections import defaultdict, deque
from dataclasses import dataclass
from datetime import datetime

import bcrypt
from fastapi import Depends, Header, HTTPException, Request
from sqlmodel import Session, select

from app.config import settings
from app.db import get_session
from app.models import ApiKey, Customer

# ---------------------------------------------------------------------------
# Password hashing (admin user)
# ---------------------------------------------------------------------------
#
# We call `bcrypt` directly rather than via `passlib.context.CryptContext`.
# Reason: `passlib==1.7.4` is incompatible with `bcrypt>=4.1` because the
# bcrypt module dropped its `__about__` attribute and tightened the 72-byte
# password limit, which trips passlib's internal `detect_wrap_bug` probe at
# import time. Using bcrypt directly is the simplest robust path.
#
# Bcrypt has a hard 72-byte input limit; we truncate proactively so that
# administrative passwords longer than 72 bytes still hash successfully (the
# extra bytes have no effect on bcrypt's strength regardless).

_BCRYPT_MAX_BYTES = 72


def _truncate_for_bcrypt(plaintext: str) -> bytes:
    """Encode and truncate to bcrypt's 72-byte input limit."""
    return plaintext.encode("utf-8")[:_BCRYPT_MAX_BYTES]


def hash_password(plaintext: str) -> str:
    """Return a bcrypt hash of `plaintext`."""
    hashed = bcrypt.hashpw(_truncate_for_bcrypt(plaintext), bcrypt.gensalt())
    return hashed.decode("utf-8")


def verify_password(plaintext: str, hashed: str) -> bool:
    """Return True iff `plaintext` matches the bcrypt hash `hashed`."""
    try:
        return bcrypt.checkpw(_truncate_for_bcrypt(plaintext), hashed.encode("utf-8"))
    except (ValueError, TypeError):
        return False


# ---------------------------------------------------------------------------
# API-Key generation & hashing
# ---------------------------------------------------------------------------

API_KEY_PLAINTEXT_PREFIX = "sk_live_"
API_KEY_PREFIX_LEN = 12


def hash_api_key(plaintext: str) -> str:
    """SHA-256 hex digest of an API key plaintext."""
    return hashlib.sha256(plaintext.encode("utf-8")).hexdigest()


def generate_api_key() -> tuple[str, str, str]:
    """Generate a new API key.

    Returns a tuple `(plaintext, hash, prefix)`:
    - `plaintext`: `sk_live_<32-url-safe-chars>`, shown to the user **once**
    - `hash`: SHA-256 hex of plaintext, stored in DB
    - `prefix`: first 12 characters of plaintext (`sk_live_xxxx`)
    """
    plaintext = API_KEY_PLAINTEXT_PREFIX + secrets.token_urlsafe(32)
    return plaintext, hash_api_key(plaintext), plaintext[:API_KEY_PREFIX_LEN]


# ---------------------------------------------------------------------------
# require_api_key dependency
# ---------------------------------------------------------------------------


@dataclass
class ApiKeyContext:
    """The authenticated principal: the API-key row + its owning customer."""

    api_key: ApiKey
    customer: Customer


async def require_api_key(
    request: Request,
    x_api_key: str = Header(..., alias="X-API-Key"),  # noqa: B008 -- FastAPI dep pattern
    session: Session = Depends(get_session),  # noqa: B008 -- FastAPI dep pattern
) -> ApiKeyContext:
    """Validate the `X-API-Key` header.

    Looks up the key by SHA-256 hash, asserts that both the key and its
    customer are active, updates `last_used_at`, and returns the
    `(ApiKey, Customer)` context. Raises 401 on any failure.
    """
    if not x_api_key:
        raise HTTPException(status_code=401, detail="Invalid or inactive API key")

    key_hash = hash_api_key(x_api_key)
    statement = (
        select(ApiKey, Customer)
        .where(ApiKey.key_hash == key_hash)
        .where(ApiKey.is_active == True)  # noqa: E712
        .join(Customer, Customer.id == ApiKey.customer_id)
        .where(Customer.is_active == True)  # noqa: E712
    )
    row = session.exec(statement).first()
    if row is None:
        raise HTTPException(status_code=401, detail="Invalid or inactive API key")

    api_key, customer = row
    api_key.last_used_at = datetime.utcnow()
    session.add(api_key)
    session.commit()
    session.refresh(api_key)

    ctx = ApiKeyContext(api_key=api_key, customer=customer)
    # Stash on request.state for downstream middleware (e.g. logging)
    request.state.api_key_ctx = ctx
    return ctx


# ---------------------------------------------------------------------------
# Rate limiting
# ---------------------------------------------------------------------------


@dataclass
class RateLimitInfo:
    """Per-request rate-limit accounting result."""

    limit: int
    remaining: int
    reset_at: int  # unix epoch seconds


class InMemoryRateLimiter:
    """Async-safe sliding-window rate limiter, keyed by API-key id.

    Each key gets a deque of timestamps for the last 60 seconds. Older
    entries are evicted on every check. Not durable across restarts —
    Redis-backed implementation is planned but not built in MVP.
    """

    WINDOW_SECONDS: int = 60

    def __init__(self, requests_per_minute: int) -> None:
        self.requests_per_minute = requests_per_minute
        self._buckets: dict[int, deque[float]] = defaultdict(deque)
        self._lock = asyncio.Lock()

    async def check(self, key_id: int) -> RateLimitInfo:
        """Record one request for `key_id`. Raises 429 on overflow."""
        async with self._lock:
            now = time.time()
            window_start = now - self.WINDOW_SECONDS
            bucket = self._buckets[key_id]

            # Evict expired timestamps
            while bucket and bucket[0] < window_start:
                bucket.popleft()

            if len(bucket) >= self.requests_per_minute:
                # Reset = when the oldest entry leaves the window
                reset_at = (
                    int(bucket[0] + self.WINDOW_SECONDS)
                    if bucket
                    else int(now + self.WINDOW_SECONDS)
                )
                raise HTTPException(
                    status_code=429,
                    detail="Rate limit exceeded",
                    headers={
                        "X-RateLimit-Limit": str(self.requests_per_minute),
                        "X-RateLimit-Remaining": "0",
                        "X-RateLimit-Reset": str(reset_at),
                    },
                )

            bucket.append(now)
            remaining = self.requests_per_minute - len(bucket)
            reset_at = int(bucket[0] + self.WINDOW_SECONDS)
            return RateLimitInfo(
                limit=self.requests_per_minute,
                remaining=remaining,
                reset_at=reset_at,
            )

    def reset(self) -> None:
        """Clear all buckets (for tests)."""
        self._buckets.clear()


rate_limiter = InMemoryRateLimiter(requests_per_minute=settings.rate_limit_per_minute)


async def enforce_rate_limit(
    ctx: ApiKeyContext = Depends(require_api_key),  # noqa: B008 -- FastAPI dep pattern
) -> ApiKeyContext:
    """Chainable dependency that runs the rate limiter for the caller's key."""
    assert ctx.api_key.id is not None  # always set after DB load
    await rate_limiter.check(ctx.api_key.id)
    return ctx
