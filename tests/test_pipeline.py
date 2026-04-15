"""Tests for ``app.services.ocr_pipeline``.

All tests mock ``httpx.Client.post`` so that no real Ollama call is made.
The PDF test is skipped automatically if ``pdftoppm``/``pdfinfo`` from
``poppler-utils`` are not available on the test runner.
"""

from __future__ import annotations

import shutil
from pathlib import Path
from unittest.mock import MagicMock, patch

import httpx
import pytest
from PIL import Image

from app.services.ocr_pipeline import (
    OcrResult,
    PageResult,
    run_ocr,
)

FIXTURES = Path(__file__).parent / "fixtures"
HAS_POPPLER = shutil.which("pdftoppm") is not None and shutil.which("pdfinfo") is not None


# ── Fixtures ──────────────────────────────────────────────────────────


@pytest.fixture
def tiny_jpg(tmp_path: Path) -> Path:
    """Create a minimal 1-page JPG fixture in a per-test tmp dir."""
    img_path = tmp_path / "tiny.jpg"
    Image.new("RGB", (800, 1000), "white").save(img_path, "JPEG", quality=90)
    return img_path


@pytest.fixture
def tiny_pdf(tmp_path: Path) -> Path:
    """Create a minimal 1-page PDF fixture in a per-test tmp dir."""
    pdf_path = tmp_path / "tiny.pdf"
    Image.new("RGB", (800, 1000), "white").save(pdf_path, "PDF")
    return pdf_path


def _mock_response(text: str = "OCR'd text for page 1") -> MagicMock:
    """Build a fake httpx Response with the given OCR ``response`` text."""
    resp = MagicMock(spec=httpx.Response)
    resp.json.return_value = {"response": text}
    resp.raise_for_status.return_value = None
    return resp


class _MockClient:
    """A minimal fake ``httpx.Client`` context manager whose ``post`` is patchable."""

    def __init__(self, *args, **kwargs) -> None:
        self._post_response = _mock_response()

    def __enter__(self) -> _MockClient:
        return self

    def __exit__(self, *exc) -> None:
        return None

    def post(self, *args, **kwargs) -> MagicMock:
        return self._post_response


# ── Tests ─────────────────────────────────────────────────────────────


def test_run_ocr_image_first_strategy_succeeds(tiny_jpg: Path, tmp_path: Path) -> None:
    """Image input: first strategy returns the mocked OCR text."""
    tmp_dir = tmp_path / "work"
    tmp_dir.mkdir()

    with patch("app.services.ocr_pipeline.httpx.Client", _MockClient):
        result = run_ocr(tiny_jpg, tmp_dir)

    assert isinstance(result, OcrResult)
    assert result.page_count == 1
    assert result.pages_ok == 1
    assert result.pages_failed == 0
    assert len(result.pages) == 1

    page = result.pages[0]
    assert page.number == 1
    assert page.text == "OCR'd text for page 1"
    # First successful strategy in STRATEGIES is "150dpi/1024px".
    assert page.strategy == "150dpi/1024px"
    assert page.elapsed_s >= 0.0


@pytest.mark.skipif(not HAS_POPPLER, reason="poppler-utils (pdftoppm/pdfinfo) not installed")
def test_run_ocr_pdf_first_strategy_succeeds(tiny_pdf: Path, tmp_path: Path) -> None:
    """PDF input: pdftoppm extracts page 1, first strategy succeeds."""
    tmp_dir = tmp_path / "work"
    tmp_dir.mkdir()

    with patch("app.services.ocr_pipeline.httpx.Client", _MockClient):
        result = run_ocr(tiny_pdf, tmp_dir)

    assert result.page_count == 1
    assert result.pages_ok == 1
    assert result.pages_failed == 0
    assert result.pages[0].text == "OCR'd text for page 1"
    assert result.pages[0].strategy == "150dpi/1024px"


def test_run_ocr_all_strategies_fail(tiny_jpg: Path, tmp_path: Path) -> None:
    """When every Ollama call raises, the page is marked as ALLE_FEHLGESCHLAGEN."""
    tmp_dir = tmp_path / "work"
    tmp_dir.mkdir()

    class _FailingClient(_MockClient):
        def post(self, *args, **kwargs):  # noqa: ARG002
            raise httpx.HTTPError("boom")

    with patch("app.services.ocr_pipeline.httpx.Client", _FailingClient):
        result = run_ocr(tiny_jpg, tmp_dir)

    assert result.page_count == 1
    assert result.pages_ok == 0
    assert result.pages_failed == 1

    page = result.pages[0]
    assert page.text is None
    assert page.strategy == "ALLE_FEHLGESCHLAGEN"
    assert page.elapsed_s == 0.0


def test_ocr_result_roundtrip() -> None:
    """``to_json_dict`` / ``from_json_dict`` is lossless."""
    original = OcrResult(
        pages=[
            PageResult(number=1, text="Hello", strategy="150dpi/1024px", elapsed_s=1.23),
            PageResult(number=2, text=None, strategy="ALLE_FEHLGESCHLAGEN", elapsed_s=0.0),
        ],
        page_count=2,
        pages_ok=1,
        pages_failed=1,
    )

    serialized = original.to_json_dict()
    restored = OcrResult.from_json_dict(serialized)

    assert restored.page_count == original.page_count
    assert restored.pages_ok == original.pages_ok
    assert restored.pages_failed == original.pages_failed
    assert len(restored.pages) == 2
    assert restored.pages[0] == original.pages[0]
    assert restored.pages[1] == original.pages[1]


def test_run_ocr_unsupported_extension(tmp_path: Path) -> None:
    """Unsupported file extensions raise a ValueError early."""
    bad = tmp_path / "notes.txt"
    bad.write_text("hello")
    tmp_dir = tmp_path / "work"
    tmp_dir.mkdir()

    with pytest.raises(ValueError, match="Unsupported input type"):
        run_ocr(bad, tmp_dir)
