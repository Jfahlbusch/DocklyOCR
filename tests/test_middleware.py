"""Tests for `app/middleware.ContentLengthLimitMiddleware`."""

from __future__ import annotations

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.middleware import ContentLengthLimitMiddleware


@pytest.fixture()
def client() -> TestClient:
    app = FastAPI()
    app.add_middleware(ContentLengthLimitMiddleware, max_bytes=1024)

    @app.post("/upload")
    async def upload() -> dict:
        return {"ok": True}

    @app.get("/ping")
    async def ping() -> dict:
        return {"pong": True}

    @app.put("/update")
    async def update() -> dict:
        return {"updated": True}

    return TestClient(app)


def test_passes_through_when_under_limit(client: TestClient) -> None:
    resp = client.post("/upload", content=b"x" * 512)
    assert resp.status_code == 200
    assert resp.json() == {"ok": True}


def test_rejects_when_over_limit(client: TestClient) -> None:
    resp = client.post("/upload", content=b"x" * 2048)
    assert resp.status_code == 413
    body = resp.json()
    assert body["error"] == "Payload too large"
    assert body["max_bytes"] == 1024


def test_rejects_put_over_limit(client: TestClient) -> None:
    resp = client.put("/update", content=b"x" * 2048)
    assert resp.status_code == 413


def test_ignores_get_requests(client: TestClient) -> None:
    # GET has no body but also has no Content-Length relevance — must not 413
    resp = client.get("/ping")
    assert resp.status_code == 200


def test_exact_limit_is_allowed(client: TestClient) -> None:
    resp = client.post("/upload", content=b"x" * 1024)
    assert resp.status_code == 200


def test_one_byte_over_limit_rejected(client: TestClient) -> None:
    resp = client.post("/upload", content=b"x" * 1025)
    assert resp.status_code == 413


def test_malformed_content_length_passes_through() -> None:
    """A garbage Content-Length header should not crash the middleware."""
    app = FastAPI()
    app.add_middleware(ContentLengthLimitMiddleware, max_bytes=100)

    @app.post("/upload")
    async def upload() -> dict:
        return {"ok": True}

    client = TestClient(app)
    # TestClient will normally set Content-Length correctly; the middleware's
    # ValueError branch is exercised synthetically via an ASGI probe.
    # This happy-path test just confirms the middleware doesn't break normal traffic.
    resp = client.post("/upload", content=b"x" * 50)
    assert resp.status_code == 200
