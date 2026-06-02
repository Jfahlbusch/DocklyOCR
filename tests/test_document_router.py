"""Tests for the engine-selection router."""

from __future__ import annotations

import subprocess
from pathlib import Path
from unittest.mock import patch

from app.services import document_router
from app.services.ocr_pipeline import OcrResult, PageResult


def _make_completed(stdout: bytes) -> subprocess.CompletedProcess:
    return subprocess.CompletedProcess(args=["pdftotext"], returncode=0, stdout=stdout, stderr=b"")


# ── select_engine ────────────────────────────────────────────────────────


def test_select_engine_image_goes_to_vllm(tmp_path: Path) -> None:
    img = tmp_path / "scan.jpg"
    img.write_bytes(b"fake-jpeg")
    assert document_router.select_engine(img) == "vllm"


def test_select_engine_png_goes_to_vllm(tmp_path: Path) -> None:
    img = tmp_path / "scan.png"
    img.write_bytes(b"fake-png")
    assert document_router.select_engine(img) == "vllm"


def test_select_engine_pdf_with_text_layer_picks_opendataloader(tmp_path: Path) -> None:
    pdf = tmp_path / "digital.pdf"
    pdf.write_bytes(b"%PDF-1.4 fake")
    # Simulate pdftotext finding ~500 chars on 1 page → above threshold
    fake_text = (b"Versicherungsbedingungen " * 30) + b"\x0c"
    with patch(
        "app.services.document_router.subprocess.run", return_value=_make_completed(fake_text)
    ):
        assert document_router.select_engine(pdf) == "opendataloader"


def test_select_engine_scanned_pdf_picks_vllm(tmp_path: Path) -> None:
    pdf = tmp_path / "scanned.pdf"
    pdf.write_bytes(b"%PDF-1.4 fake")
    # Almost no extractable text → looks like a scan → vllm
    with patch(
        "app.services.document_router.subprocess.run", return_value=_make_completed(b"   \x0c")
    ):
        assert document_router.select_engine(pdf) == "vllm"


def test_select_engine_pdftotext_error_falls_back_to_vllm(tmp_path: Path) -> None:
    pdf = tmp_path / "broken.pdf"
    pdf.write_bytes(b"corrupt")
    with patch(
        "app.services.document_router.subprocess.run",
        side_effect=subprocess.CalledProcessError(1, "pdftotext"),
    ):
        assert document_router.select_engine(pdf) == "vllm"


def test_select_engine_unknown_extension_picks_vllm(tmp_path: Path) -> None:
    f = tmp_path / "weird.docx"
    f.write_bytes(b"x")
    assert document_router.select_engine(f) == "vllm"


# ── is_result_acceptable ─────────────────────────────────────────────────


def test_is_result_acceptable_empty_result_is_not_acceptable() -> None:
    r = OcrResult(pages=[], page_count=0, pages_ok=0, pages_failed=0)
    assert document_router.is_result_acceptable(r) is False


def test_is_result_acceptable_all_empty_pages_is_not_acceptable() -> None:
    r = OcrResult(
        pages=[PageResult(number=1, text=None, strategy="opendataloader", elapsed_s=0.1)],
        page_count=1,
        pages_ok=0,
        pages_failed=1,
    )
    assert document_router.is_result_acceptable(r) is False


def test_is_result_acceptable_sparse_result_is_rejected() -> None:
    # Only 10 chars on the page → below threshold → reject
    r = OcrResult(
        pages=[PageResult(number=1, text="hi there", strategy="opendataloader", elapsed_s=0.1)],
        page_count=1,
        pages_ok=1,
        pages_failed=0,
    )
    assert document_router.is_result_acceptable(r) is False


def test_is_result_acceptable_typical_avb_page_passes() -> None:
    # A realistic AVB page is on the order of 1-3k characters
    text = "Allgemeine Versicherungsbedingungen " * 50  # ~1800 chars
    r = OcrResult(
        pages=[PageResult(number=1, text=text, strategy="opendataloader", elapsed_s=0.1)],
        page_count=1,
        pages_ok=1,
        pages_failed=0,
    )
    assert document_router.is_result_acceptable(r) is True
