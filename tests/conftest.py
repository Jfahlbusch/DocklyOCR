"""Shared pytest fixtures for the DocklyOCR test suite.

Adds a session-scoped auto fixture that (re)generates ``tests/fixtures/sample.pdf``
if it's missing. This keeps the fixture PDF out of the repo as a committed
binary blob while still being available during every test run.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from PIL import Image, ImageDraw

FIXTURES_DIR = Path(__file__).parent / "fixtures"
SAMPLE_PDF = FIXTURES_DIR / "sample.pdf"


def _make_sample_pdf(path: Path) -> None:
    """Generate a real 2-page PDF via Pillow."""
    path.parent.mkdir(parents=True, exist_ok=True)
    pages: list[Image.Image] = []
    for i in (1, 2):
        img = Image.new("RGB", (800, 1100), "white")
        draw = ImageDraw.Draw(img)
        draw.text((80, 100), f"DocklyOCR Test Page {i}", fill="black")
        draw.text((80, 200), "The quick brown fox jumps over the lazy dog.", fill="black")
        pages.append(img)
    pages[0].save(str(path), "PDF", save_all=True, append_images=pages[1:])


@pytest.fixture(scope="session", autouse=True)
def _ensure_sample_pdf() -> Path:
    """Create ``tests/fixtures/sample.pdf`` once per session if missing."""
    if not SAMPLE_PDF.exists():
        _make_sample_pdf(SAMPLE_PDF)
    return SAMPLE_PDF
