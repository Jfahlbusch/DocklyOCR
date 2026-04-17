"""Tests for ``app.services.ocr_pipeline``.

All tests mock ``httpx.Client.post`` so that no real backend call is made.
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
    """Build a fake httpx Response mimicking vLLM's OpenAI-compatible chat completion."""
    resp = MagicMock(spec=httpx.Response)
    resp.json.return_value = {"choices": [{"message": {"content": text}}]}
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
    """When every backend call raises, the page is marked as ALLE_FEHLGESCHLAGEN."""
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


def test_page_result_is_table_default():
    pr = PageResult(number=1, text="hello", strategy="150dpi/1024px", elapsed_s=1.0)
    assert pr.is_table is False


def test_page_result_is_table_explicit():
    pr = PageResult(
        number=1, text="|A|B|\n|1|2|", strategy="150dpi/1024px", elapsed_s=1.0, is_table=True
    )
    assert pr.is_table is True


def test_ocr_result_roundtrip_with_is_table():
    pages = [
        PageResult(1, "normal text", "150dpi/1024px", 2.0, is_table=False),
        PageResult(2, "|A|B|\n|1|2|", "150dpi/1024px", 3.0, is_table=True),
    ]
    result = OcrResult(pages=pages, page_count=2, pages_ok=2, pages_failed=0)
    data = result.to_json_dict()
    restored = OcrResult.from_json_dict(data)
    assert restored.pages[0].is_table is False
    assert restored.pages[1].is_table is True


def test_ocr_result_from_json_dict_missing_is_table():
    data = {
        "page_count": 1,
        "pages_ok": 1,
        "pages_failed": 0,
        "pages": [{"number": 1, "text": "hello", "strategy": "s", "elapsed_s": 1.0}],
    }
    result = OcrResult.from_json_dict(data)
    assert result.pages[0].is_table is False


def test_detect_table_patterns_positive():
    from app.services.ocr_pipeline import _detect_table_patterns

    text = "| Leistung | Betrag | SB |\n|----------|--------|----|\n| Haftpflicht | 5.000.000 EUR | 500 EUR |\n| Kasko | 50.000 EUR | 300 EUR |\n| Glasbruch | 10.000 EUR | 150 EUR |"
    assert _detect_table_patterns(text) is True


def test_detect_table_patterns_negative():
    from app.services.ocr_pipeline import _detect_table_patterns

    text = "Die Versicherung gilt für alle Sachschäden, die durch\nhöhere Gewalt verursacht werden. Der Versicherungsnehmer\nist verpflichtet, den Schaden innerhalb von 14 Tagen\nnach Bekanntwerden zu melden."
    assert _detect_table_patterns(text) is False


def test_detect_table_patterns_short_text():
    from app.services.ocr_pipeline import _detect_table_patterns

    assert _detect_table_patterns("| A | B |") is False
    assert _detect_table_patterns("") is False


def test_detect_table_patterns_number_columns():
    from app.services.ocr_pipeline import _detect_table_patterns

    text = "Posten          Betrag       Steuer\nGrundbeitrag    1.234,56     234\nZusatzbeitrag     567,89     107\nGesamtbeitrag   1.802,45     341"
    assert _detect_table_patterns(text) is True


def test_ocr_table_returns_markdown(monkeypatch, tmp_path):
    from app.services.ocr_pipeline import _ocr_table

    img_path = tmp_path / "test_table.jpg"
    Image.new("RGB", (100, 100), "white").save(img_path, "JPEG")

    class FakeResponse:
        status_code = 200

        def raise_for_status(self):
            pass

        def json(self):
            return {"choices": [{"message": {"content": "| A | B |\n|---|---|\n| 1 | 2 |"}}]}

    class FakeClient:
        def __init__(self, **kw):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *a):
            pass

        def post(self, url, **kw):
            # Assert the table-specific prompt is sent as the text part
            # of the multimodal chat message.
            text_parts = [
                c["text"] for c in kw["json"]["messages"][0]["content"] if c.get("type") == "text"
            ]
            assert any("Markdown table" in t for t in text_parts)
            return FakeResponse()

    monkeypatch.setattr("app.services.ocr_pipeline.httpx.Client", FakeClient)
    result = _ocr_table(img_path)
    assert "| A | B |" in result


needs_pdftoppm = pytest.mark.skipif(
    shutil.which("pdftoppm") is None, reason="pdftoppm not installed"
)


@needs_pdftoppm
def test_batch_extract_pages(tmp_path):
    from PIL import Image

    from app.services.ocr_pipeline import _batch_extract_pages

    imgs = [Image.new("RGB", (200, 300), color) for color in ("white", "gray")]
    pdf_path = tmp_path / "test.pdf"
    imgs[0].save(pdf_path, "PDF", save_all=True, append_images=imgs[1:])
    pages = _batch_extract_pages(pdf_path, tmp_path, dpi=150)
    assert len(pages) == 2
    assert all(p.suffix == ".jpg" for p in pages)
    assert all(p.exists() for p in pages)
    assert pages[0].name < pages[1].name


def test_downscale_for_strategy(tmp_path):
    from PIL import Image

    from app.services.ocr_pipeline import _downscale_for_strategy

    src = tmp_path / "page-001.jpg"
    Image.new("RGB", (1500, 2000), "white").save(src, "JPEG")
    scaled = _downscale_for_strategy(src, target_dpi=100, tmp_dir=tmp_path)
    assert scaled.exists()
    img = Image.open(scaled)
    assert 990 <= img.width <= 1010
    assert 1325 <= img.height <= 1340


# ── Cross-page boundary merge tests ──────────────────────────────────


def test_merge_across_boundaries_joins_broken_sentence():
    from app.services.ocr_pipeline import _merge_across_boundaries

    pages = [
        PageResult(1, "Die Versicherung gilt für alle", "s", 1.0),
        PageResult(2, "Sachschäden ab 500 EUR.", "s", 1.0),
    ]
    _merge_across_boundaries(pages)
    assert pages[0].text == ""
    assert pages[1].text == "Die Versicherung gilt für alle Sachschäden ab 500 EUR."


def test_merge_across_boundaries_keeps_complete_sentences():
    from app.services.ocr_pipeline import _merge_across_boundaries

    pages = [
        PageResult(1, "Erster Absatz endet hier.", "s", 1.0),
        PageResult(2, "Zweiter Absatz beginnt.", "s", 1.0),
    ]
    _merge_across_boundaries(pages)
    assert pages[0].text == "Erster Absatz endet hier."
    assert pages[1].text == "Zweiter Absatz beginnt."


def test_merge_across_boundaries_skips_section_start():
    from app.services.ocr_pipeline import _merge_across_boundaries

    pages = [
        PageResult(1, "Ende des vorherigen Textes ohne Punkt", "s", 1.0),
        PageResult(2, "§ 5 Ausschlüsse\nDie Versicherung...", "s", 1.0),
    ]
    _merge_across_boundaries(pages)
    assert "ohne Punkt" in pages[0].text
    assert pages[1].text.startswith("§ 5")


def test_merge_across_boundaries_skips_table_pages():
    from app.services.ocr_pipeline import _merge_across_boundaries

    pages = [
        PageResult(1, "Text endet ohne Punkt", "s", 1.0),
        PageResult(2, "| A | B |", "s", 1.0, is_table=True),
    ]
    _merge_across_boundaries(pages)
    assert pages[0].text == "Text endet ohne Punkt"
    assert pages[1].text == "| A | B |"


def test_merge_across_boundaries_skips_none():
    from app.services.ocr_pipeline import _merge_across_boundaries

    pages = [
        PageResult(1, "Text ohne Punkt", "s", 1.0),
        PageResult(2, None, "ALLE_FEHLGESCHLAGEN", 0.0),
        PageResult(3, "Neuer Absatz.", "s", 1.0),
    ]
    _merge_across_boundaries(pages)
    assert pages[0].text == "Text ohne Punkt"
    assert pages[1].text is None
    assert pages[2].text == "Neuer Absatz."


def test_merge_three_pages_chain():
    from app.services.ocr_pipeline import _merge_across_boundaries

    pages = [
        PageResult(1, "Absatz eins beginnt und", "s", 1.0),
        PageResult(2, "geht weiter bis", "s", 1.0),
        PageResult(3, "zum Ende dieses Satzes.", "s", 1.0),
    ]
    _merge_across_boundaries(pages)
    assert "zum Ende dieses Satzes." in pages[2].text


def test_incremental_writer_md(tmp_path):
    from app.services.ocr_pipeline import IncrementalWriter

    out = tmp_path / "result.md"
    writer = IncrementalWriter(out, "md")
    writer.append_chunk(
        [
            PageResult(1, "Erster Absatz.", "150dpi/1024px", 2.0),
            PageResult(2, "| A | B |", "150dpi/1024px", 1.0, is_table=True),
        ]
    )
    writer.append_chunk(
        [
            PageResult(3, "Dritter Absatz.", "100dpi/768px", 3.0),
        ]
    )
    content = out.read_text(encoding="utf-8")
    assert "## Seite 1" in content
    assert "## Seite 2" in content
    assert "## Seite 3" in content
    assert "Erster Absatz." in content
    assert "| A | B |" in content
    assert content.count("> OCR-Strategie:") == 2  # pages 1 and 3
    assert "`150dpi/1024px`" in content
    assert "`100dpi/768px`" in content


def test_incremental_writer_txt(tmp_path):
    from app.services.ocr_pipeline import IncrementalWriter

    out = tmp_path / "result.txt"
    writer = IncrementalWriter(out, "txt")
    writer.append_chunk(
        [
            PageResult(1, "Page one.", "s", 1.0),
            PageResult(2, "Page two.", "s", 1.0),
        ]
    )
    content = out.read_text(encoding="utf-8")
    assert "Page one." in content
    assert "Page two." in content


def test_incremental_writer_json_deferred(tmp_path):
    from app.services.ocr_pipeline import IncrementalWriter

    out = tmp_path / "result.json"
    writer = IncrementalWriter(out, "json")
    writer.append_chunk([PageResult(1, "text", "s", 1.0)])
    assert out.read_text() == ""
    result = OcrResult(
        pages=[PageResult(1, "text", "s", 1.0)],
        page_count=1,
        pages_ok=1,
        pages_failed=0,
    )
    body = writer.finalize(result)
    assert b'"page_count": 1' in body


def test_incremental_writer_failed_page_md(tmp_path):
    from app.services.ocr_pipeline import IncrementalWriter

    out = tmp_path / "result.md"
    writer = IncrementalWriter(out, "md")
    writer.append_chunk([PageResult(1, None, "ALLE_FEHLGESCHLAGEN", 0.0)])
    content = out.read_text(encoding="utf-8")
    assert "[OCR-Fehler auf Seite 1]" in content


# ── v5 run_ocr integration tests ────────────────────────────────────


def test_run_ocr_with_output_path_writes_incrementally(tmp_path, monkeypatch):
    from app.services.ocr_pipeline import run_ocr

    call_count = {"n": 0}

    class FakeResponse:
        status_code = 200

        def raise_for_status(self):
            pass

        def json(self):  # noqa: ARG002
            call_count["n"] += 1
            return {"choices": [{"message": {"content": f"Page {call_count['n']} text."}}]}

    class FakeClient:
        def __init__(self, **kw):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *a):
            pass

        def post(self, url, **kw):
            return FakeResponse()

    monkeypatch.setattr("app.services.ocr_pipeline.httpx.Client", FakeClient)

    img_path = tmp_path / "input.jpg"
    Image.new("RGB", (200, 300), "white").save(img_path, "JPEG")

    output_path = tmp_path / "result.md"
    result = run_ocr(img_path, tmp_path, output_path=output_path, output_format="md")

    assert result.page_count == 1
    assert result.pages_ok == 1
    assert output_path.exists()
    content = output_path.read_text()
    assert "## Seite 1" in content


def test_run_ocr_without_output_path_backward_compat(tmp_path, monkeypatch):
    from app.services.ocr_pipeline import run_ocr

    class FakeResponse:
        status_code = 200

        def raise_for_status(self):
            pass

        def json(self):
            return {"choices": [{"message": {"content": "Some text."}}]}

    class FakeClient:
        def __init__(self, **kw):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *a):
            pass

        def post(self, url, **kw):
            return FakeResponse()

    monkeypatch.setattr("app.services.ocr_pipeline.httpx.Client", FakeClient)

    img_path = tmp_path / "input.jpg"
    Image.new("RGB", (200, 300), "white").save(img_path, "JPEG")

    result = run_ocr(img_path, tmp_path)
    assert result.page_count == 1
    assert result.pages_ok == 1
    assert result.pages[0].text == "Some text."
