"""Decides which OCR engine to use for a given input.

Two engines exist today:

* ``opendataloader`` — deterministic, CPU-only, fast (~3s for a typical
  AVB), used when the input is a digital PDF that already has a text
  layer. No GPU activation needed.
* ``vllm`` — the vision-LLM pipeline (Qwen2.5-VL on H100 / L40S fallback).
  Used for images and for scanned PDFs without a text layer.

The router also exposes a heuristic to judge whether an
opendataloader run produced a usable result — if not, the worker can
fall back to ``vllm`` for the same document.
"""

from __future__ import annotations

import logging
import subprocess
from pathlib import Path
from typing import Literal

from app.services.ocr_pipeline import OcrResult

logger = logging.getLogger(__name__)

Engine = Literal["opendataloader", "vllm"]

_IMAGE_SUFFIXES = {".jpg", ".jpeg", ".png", ".tif", ".tiff"}
_PDF_SUFFIXES = {".pdf"}

# A digital PDF typically has hundreds of characters per page in its
# text layer; scans have virtually none (only watermarks/metadata).
# 100 is a forgiving threshold that still catches mostly-image PDFs.
_MIN_CHARS_PER_PAGE_FOR_TEXT_LAYER = 100

# After an opendataloader run, the result is considered usable only if
# each page has at least this many characters of extracted text on
# average. Below this, we assume the PDF is mostly scanned or that the
# extraction failed → fall back to the vision pipeline.
_MIN_CHARS_PER_PAGE_FOR_ACCEPTABLE_RESULT = 50


def select_engine(input_path: Path) -> Engine:
    """Pick the best engine for the given input file.

    * Image files → ``vllm`` (opendataloader is PDF-only)
    * PDF with a real text layer → ``opendataloader``
    * Anything else (scanned PDF, unknown) → ``vllm``
    """
    suffix = input_path.suffix.lower()
    if suffix in _IMAGE_SUFFIXES:
        return "vllm"
    if suffix in _PDF_SUFFIXES and _pdf_has_text_layer(input_path):
        return "opendataloader"
    return "vllm"


def _pdf_has_text_layer(
    pdf_path: Path,
    min_chars_per_page: int = _MIN_CHARS_PER_PAGE_FOR_TEXT_LAYER,
) -> bool:
    """True if ``pdftotext`` extracts substantial text from the PDF.

    Uses ``pdftotext -q`` (already shipped in the API container via
    ``poppler-utils``). Failure → return False so we err on the side of
    the more robust vision pipeline.
    """
    try:
        completed = subprocess.run(
            ["pdftotext", "-q", str(pdf_path), "-"],
            capture_output=True,
            timeout=30,
            check=True,
        )
    except (subprocess.SubprocessError, FileNotFoundError) as e:
        logger.warning("pdftotext probe failed for %s: %s — defaulting to vllm", pdf_path.name, e)
        return False

    text = completed.stdout.decode("utf-8", errors="ignore")
    # pdftotext separates pages with form-feed (\x0c). Count pages by
    # separators (final page has no trailing FF).
    pages = max(1, text.count("\x0c") + (0 if text.endswith("\x0c") else 1))
    chars_per_page = len(text) / pages
    has_layer = chars_per_page >= min_chars_per_page
    logger.debug(
        "text-layer probe %s: %d pages, %.0f chars/page → has_layer=%s",
        pdf_path.name,
        pages,
        chars_per_page,
        has_layer,
    )
    return has_layer


def is_result_acceptable(
    result: OcrResult,
    min_chars_per_page: int = _MIN_CHARS_PER_PAGE_FOR_ACCEPTABLE_RESULT,
) -> bool:
    """Heuristic to decide if an opendataloader run produced usable output.

    Returns False when the average extracted text per page is suspiciously
    low — typical sign that the PDF was actually mostly images and our
    text-layer probe was fooled by stray metadata text.
    """
    if result.page_count == 0:
        return False
    if result.pages_ok == 0:
        return False
    total_chars = sum(len(p.text) for p in result.pages if p.text)
    chars_per_page = total_chars / result.page_count
    acceptable = chars_per_page >= min_chars_per_page
    if not acceptable:
        logger.info(
            "opendataloader result rejected: only %.0f chars/page avg "
            "(threshold %d) → falling back to vllm",
            chars_per_page,
            min_chars_per_page,
        )
    return acceptable
