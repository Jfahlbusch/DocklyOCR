"""Unit tests for the multi-GPU fallback logic in gpu_manager.py."""

from __future__ import annotations

from unittest.mock import patch

import pytest

from app.services import gpu_manager


class _FakeResponse:
    def __init__(self, status_code: int, text: str = ""):
        self.status_code = status_code
        self.text = text


def _patch_settings(monkeypatch, **overrides):
    """Replace fields on the module-level ``settings`` for one test."""
    for key, value in overrides.items():
        monkeypatch.setattr(f"app.services.gpu_manager.settings.{key}", value)


# ── _scw_poweron ────────────────────────────────────────────────────────


def test_scw_poweron_returns_ok_on_2xx(monkeypatch):
    _patch_settings(monkeypatch, scw_secret_key="secret")
    with patch("app.services.gpu_manager.httpx.post") as mock_post:
        mock_post.return_value = _FakeResponse(200, "")
        assert gpu_manager._scw_poweron("srv-id", "fr-par-2") == "ok"


def test_scw_poweron_detects_out_of_stock(monkeypatch):
    _patch_settings(monkeypatch, scw_secret_key="secret")
    with patch("app.services.gpu_manager.httpx.post") as mock_post:
        mock_post.return_value = _FakeResponse(
            412, '{"type": "out_of_stock", "resource": "H100-1-80G"}'
        )
        assert gpu_manager._scw_poweron("srv-id", "fr-par-2") == "out_of_stock"


def test_scw_poweron_returns_error_on_other_http_failure(monkeypatch):
    _patch_settings(monkeypatch, scw_secret_key="secret")
    with patch("app.services.gpu_manager.httpx.post") as mock_post:
        mock_post.return_value = _FakeResponse(500, "internal error")
        assert gpu_manager._scw_poweron("srv-id", "fr-par-2") == "error"


def test_scw_poweron_412_without_stock_marker_is_plain_error(monkeypatch):
    """412 can mean other precondition failures (e.g. already running)."""
    _patch_settings(monkeypatch, scw_secret_key="secret")
    with patch("app.services.gpu_manager.httpx.post") as mock_post:
        mock_post.return_value = _FakeResponse(412, "server must be stopped")
        assert gpu_manager._scw_poweron("srv-id", "fr-par-2") == "error"


# ── ensure_any_gpu_running: fast-path ──────────────────────────────────


def test_ensure_any_returns_primary_url_when_already_ready(monkeypatch):
    _patch_settings(
        monkeypatch,
        scw_access_key="ak",
        scw_secret_key="sk",
        scw_gpu_server_id="srv-primary",
        scw_gpu_zone="fr-par-2",
        backend_url="http://primary:8000",
        scw_gpu_server_id_fallback="",
        backend_url_fallback="",
    )
    with (
        patch("app.services.gpu_manager._backend_ready", return_value=True),
        patch("app.services.gpu_manager._backend_serves_inference", return_value=True),
        patch("app.services.gpu_manager._scw_poweron") as mock_poweron,
    ):
        assert gpu_manager.ensure_any_gpu_running() == ("http://primary:8000", "primary")
        mock_poweron.assert_not_called()


# ── ensure_any_gpu_running: fallback on out_of_stock ───────────────────


def test_ensure_any_falls_back_when_primary_out_of_stock(monkeypatch):
    _patch_settings(
        monkeypatch,
        scw_access_key="ak",
        scw_secret_key="sk",
        scw_gpu_server_id="srv-primary",
        scw_gpu_zone="fr-par-2",
        backend_url="http://primary:8000",
        scw_gpu_server_id_fallback="srv-fallback",
        scw_gpu_zone_fallback="",
        backend_url_fallback="http://fallback:8000",
    )
    # Neither ready initially
    with (
        patch("app.services.gpu_manager._backend_ready", return_value=False),
        patch("app.services.gpu_manager._backend_serves_inference", return_value=False),
        patch(
            "app.services.gpu_manager._scw_poweron",
            side_effect=["out_of_stock", "ok"],
        ) as mock_poweron,
        patch("app.services.gpu_manager._verify_poweron_holds", return_value=True) as mock_verify,
        patch(
            "app.services.gpu_manager._wait_for_backend",
            return_value="http://fallback:8000",
        ) as mock_wait,
    ):
        result = gpu_manager.ensure_any_gpu_running()
        assert result == ("http://fallback:8000", "fallback")
        # Primary attempted first, fallback second
        assert mock_poweron.call_count == 2
        mock_poweron.assert_any_call("srv-primary", "fr-par-2")
        mock_poweron.assert_any_call("srv-fallback", "fr-par-2")
        # Hold-check only on the second (successful) candidate
        mock_verify.assert_called_once_with("srv-fallback", "fr-par-2")
        # _wait_for_backend only called once (for the successful candidate)
        mock_wait.assert_called_once_with("http://fallback:8000")


def test_ensure_any_falls_back_when_primary_reverts_after_poweron(monkeypatch):
    """Scaleway-internal abort: poweron returns ok but server flips back."""
    _patch_settings(
        monkeypatch,
        scw_access_key="ak",
        scw_secret_key="sk",
        scw_gpu_server_id="srv-primary",
        scw_gpu_zone="fr-par-2",
        backend_url="http://primary:8000",
        scw_gpu_server_id_fallback="srv-fallback",
        scw_gpu_zone_fallback="",
        backend_url_fallback="http://fallback:8000",
    )
    with (
        patch("app.services.gpu_manager._backend_ready", return_value=False),
        patch("app.services.gpu_manager._backend_serves_inference", return_value=False),
        patch("app.services.gpu_manager._scw_poweron", return_value="ok"),
        # Primary reverts → False, fallback holds → True
        patch(
            "app.services.gpu_manager._verify_poweron_holds",
            side_effect=[False, True],
        ) as mock_verify,
        patch(
            "app.services.gpu_manager._wait_for_backend",
            return_value="http://fallback:8000",
        ) as mock_wait,
    ):
        result = gpu_manager.ensure_any_gpu_running()
        assert result == ("http://fallback:8000", "fallback")
        assert mock_verify.call_count == 2
        # _wait_for_backend NOT called for primary because verify said False
        mock_wait.assert_called_once_with("http://fallback:8000")


def test_verify_poweron_holds_returns_false_on_archived_state(monkeypatch):
    _patch_settings(monkeypatch, scw_secret_key="secret")
    # Returns running first, then archived → should fail-fast
    with (
        patch(
            "app.services.gpu_manager._scw_get_state",
            side_effect=["starting", "starting", "archived"],
        ),
        patch("app.services.gpu_manager.time.sleep"),
    ):
        assert gpu_manager._verify_poweron_holds("srv", "fr-par-2", hold_seconds=10) is False


def test_verify_poweron_holds_returns_true_when_state_stays_up(monkeypatch):
    _patch_settings(monkeypatch, scw_secret_key="secret")
    with (
        patch("app.services.gpu_manager._scw_get_state", return_value="running"),
        patch("app.services.gpu_manager.time.sleep"),
        patch(
            "app.services.gpu_manager.time.time",
            side_effect=[0.0, 1.0, 2.0, 3.0, 4.0, 11.0],
        ),
    ):
        assert gpu_manager._verify_poweron_holds("srv", "fr-par-2", hold_seconds=10) is True


def test_scw_get_state_returns_unknown_on_http_error(monkeypatch):
    _patch_settings(monkeypatch, scw_secret_key="secret")
    with patch("app.services.gpu_manager.httpx.get") as mock_get:
        mock_get.side_effect = __import__("httpx").HTTPError("boom")
        assert gpu_manager._scw_get_state("srv", "fr-par-2") == "unknown"


def test_ensure_any_raises_when_all_candidates_out_of_stock(monkeypatch):
    _patch_settings(
        monkeypatch,
        scw_access_key="ak",
        scw_secret_key="sk",
        scw_gpu_server_id="srv-primary",
        scw_gpu_zone="fr-par-2",
        backend_url="http://primary:8000",
        scw_gpu_server_id_fallback="srv-fallback",
        scw_gpu_zone_fallback="",
        backend_url_fallback="http://fallback:8000",
    )
    with (
        patch("app.services.gpu_manager._backend_ready", return_value=False),
        patch("app.services.gpu_manager._backend_serves_inference", return_value=False),
        patch("app.services.gpu_manager._scw_poweron", return_value="out_of_stock"),
        pytest.raises(RuntimeError, match="No GPU became ready"),
    ):
        gpu_manager.ensure_any_gpu_running()


def test_ensure_any_tries_fallback_when_primary_boot_times_out(monkeypatch):
    _patch_settings(
        monkeypatch,
        scw_access_key="ak",
        scw_secret_key="sk",
        scw_gpu_server_id="srv-primary",
        scw_gpu_zone="fr-par-2",
        backend_url="http://primary:8000",
        scw_gpu_server_id_fallback="srv-fallback",
        scw_gpu_zone_fallback="",
        backend_url_fallback="http://fallback:8000",
    )
    with (
        patch("app.services.gpu_manager._backend_ready", return_value=False),
        patch("app.services.gpu_manager._backend_serves_inference", return_value=False),
        patch("app.services.gpu_manager._scw_poweron", return_value="ok"),
        patch("app.services.gpu_manager._verify_poweron_holds", return_value=True),
        patch(
            "app.services.gpu_manager._wait_for_backend",
            side_effect=[RuntimeError("timeout"), "http://fallback:8000"],
        ) as mock_wait,
    ):
        result = gpu_manager.ensure_any_gpu_running()
        assert result == ("http://fallback:8000", "fallback")
        assert mock_wait.call_count == 2


# ── gpu_candidates property ─────────────────────────────────────────────


def test_gpu_candidates_primary_only(monkeypatch):
    from app.config import settings

    _patch_settings(
        monkeypatch,
        scw_gpu_server_id="srv-primary",
        scw_gpu_zone="fr-par-2",
        backend_url="http://primary:8000",
        scw_gpu_server_id_fallback="",
        backend_url_fallback="",
    )
    # Also patch on the actual settings instance used by the property
    monkeypatch.setattr(settings, "scw_gpu_server_id", "srv-primary")
    monkeypatch.setattr(settings, "scw_gpu_zone", "fr-par-2")
    monkeypatch.setattr(settings, "backend_url", "http://primary:8000")
    monkeypatch.setattr(settings, "scw_gpu_server_id_fallback", "")
    monkeypatch.setattr(settings, "backend_url_fallback", "")

    cands = settings.gpu_candidates
    assert len(cands) == 1
    assert cands[0] == ("primary", "srv-primary", "fr-par-2", "http://primary:8000", "primary")


def test_gpu_candidates_primary_and_fallback(monkeypatch):
    from app.config import settings

    monkeypatch.setattr(settings, "scw_gpu_server_id", "srv-primary")
    monkeypatch.setattr(settings, "scw_gpu_zone", "fr-par-2")
    monkeypatch.setattr(settings, "backend_url", "http://primary:8000")
    monkeypatch.setattr(settings, "scw_gpu_server_id_fallback", "srv-fb")
    monkeypatch.setattr(settings, "scw_gpu_zone_fallback", "")
    monkeypatch.setattr(settings, "backend_url_fallback", "http://fb:8000")

    cands = settings.gpu_candidates
    assert len(cands) == 2
    assert cands[0][0] == "primary"
    assert cands[1] == ("fallback", "srv-fb", "fr-par-2", "http://fb:8000", "fallback")


def test_gpu_candidates_fallback_zone_override(monkeypatch):
    from app.config import settings

    monkeypatch.setattr(settings, "scw_gpu_server_id", "srv-primary")
    monkeypatch.setattr(settings, "scw_gpu_zone", "fr-par-2")
    monkeypatch.setattr(settings, "backend_url", "http://primary:8000")
    monkeypatch.setattr(settings, "scw_gpu_server_id_fallback", "srv-fb")
    monkeypatch.setattr(settings, "scw_gpu_zone_fallback", "nl-ams-1")
    monkeypatch.setattr(settings, "backend_url_fallback", "http://fb:8000")

    assert settings.gpu_candidates[1][2] == "nl-ams-1"
