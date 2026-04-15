"""Tests for ``app.services.formatters``."""

from __future__ import annotations

import json

import pytest

from app.services.formatters import format_output
from app.services.ocr_pipeline import OcrResult, PageResult


@pytest.fixture
def sample_result() -> OcrResult:
    return OcrResult(
        pages=[
            PageResult(
                number=1,
                text="Seite 1 Inhalt § 1 Allgemeines\n§ 2 Details",
                strategy="150dpi/1024px",
                elapsed_s=2.5,
            ),
            PageResult(number=2, text=None, strategy="ALLE_FEHLGESCHLAGEN", elapsed_s=0.0),
            PageResult(
                number=3,
                text="Abschließend: Seite 3 Text",
                strategy="100dpi/768px/gray",
                elapsed_s=3.1,
            ),
        ],
        page_count=3,
        pages_ok=2,
        pages_failed=1,
    )


# ── Markdown ──────────────────────────────────────────────────────────


def test_format_md(sample_result: OcrResult) -> None:
    body, mime = format_output(sample_result, "md")

    assert isinstance(body, bytes)
    assert mime == "text/markdown; charset=utf-8"

    text = body.decode("utf-8")

    # Page headers
    assert "## Seite 1" in text
    assert "## Seite 2" in text
    assert "## Seite 3" in text

    # Strategy footnote on OK pages only
    assert "OCR-Strategie: `150dpi/1024px`" in text
    assert "OCR-Strategie: `100dpi/768px/gray`" in text
    # Failed page does NOT get a strategy line — only its OK siblings do.
    assert "OCR-Strategie: `ALLE_FEHLGESCHLAGEN`" not in text

    # Failure marker on page 2
    assert "[OCR-Fehler auf Seite 2" in text


# ── Plain text ────────────────────────────────────────────────────────


def test_format_txt(sample_result: OcrResult) -> None:
    body, mime = format_output(sample_result, "txt")

    assert isinstance(body, bytes)
    assert mime == "text/plain; charset=utf-8"

    # Form-feed byte separates pages
    assert b"\x0c" in body

    text = body.decode("utf-8")
    assert "Seite 1 Inhalt" in text
    assert "[OCR-Fehler Seite 2]" in text
    assert "Abschließend: Seite 3 Text" in text
    # No strategy annotation in txt output
    assert "150dpi/1024px" not in text


# ── TOON ──────────────────────────────────────────────────────────────


def test_format_toon(sample_result: OcrResult) -> None:
    body, mime = format_output(sample_result, "toon")

    assert isinstance(body, bytes)
    assert mime == "application/x-toon; charset=utf-8"

    text = body.decode("utf-8")

    assert text.startswith("document:")
    assert "  title: document" in text
    assert "  type: legal_document" in text
    assert "  pages: 3" in text

    assert "page[1]:" in text
    # § section detection on page 1
    assert "§ 1" in text or "§1:" in text or "§ 1:" in text
    # The Anhang C splitter preserves the literal "§ 1" (with space) — accept either form.
    assert "  § 1:" in text or "  §1:" in text

    # Page 2 failure marker
    assert "page[2]:" in text
    assert "  text: [OCR-Fehler]" in text

    # Page 3 present
    assert "page[3]:" in text


# ── JSON ──────────────────────────────────────────────────────────────


def test_format_json(sample_result: OcrResult) -> None:
    body, mime = format_output(sample_result, "json")

    assert isinstance(body, bytes)
    assert mime == "application/json; charset=utf-8"

    payload = json.loads(body.decode("utf-8"))

    assert payload["meta"]["page_count"] == 3
    assert payload["meta"]["pages_ok"] == 2
    assert payload["meta"]["pages_failed"] == 1

    assert len(payload["pages"]) == 3
    assert payload["pages"][0]["number"] == 1
    assert payload["pages"][0]["strategy"] == "150dpi/1024px"
    assert payload["pages"][1]["text"] is None
    assert payload["pages"][2]["strategy"] == "100dpi/768px/gray"


# ── Unknown format ────────────────────────────────────────────────────


def test_format_unknown_raises(sample_result: OcrResult) -> None:
    with pytest.raises(ValueError, match="Unknown output format"):
        format_output(sample_result, "xml")


# ── Invariants across all formats ─────────────────────────────────────


@pytest.mark.parametrize("fmt", ["md", "txt", "toon", "json"])
def test_format_returns_bytes_and_mime(sample_result: OcrResult, fmt: str) -> None:
    body, mime = format_output(sample_result, fmt)
    assert isinstance(body, bytes)
    assert isinstance(mime, str)
    assert len(mime) > 0
    assert len(body) > 0
