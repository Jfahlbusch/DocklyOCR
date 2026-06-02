"""Tests for the opendataloader pipeline wrapper.

We mock ``opendataloader_pdf.convert`` so the tests run without Java —
the wrapper's responsibility is to (a) call the convert function with
the right args, (b) read the produced markdown file, (c) split on the
sentinel, and (d) build an ``OcrResult`` with the right shape.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

from app.services.opendataloader_pipeline import _PAGE_SEPARATOR, run_opendataloader


def _fake_convert_factory(markdown_body: str, output_stem: str):
    """Build a stand-in for ``opendataloader_pdf.convert`` that just writes
    ``markdown_body`` to ``{output_dir}/{stem}.md``."""

    def _fake_convert(input_path, output_dir, **_kwargs):  # noqa: ANN001
        out = Path(output_dir) / f"{output_stem}.md"
        out.write_text(markdown_body, encoding="utf-8")

    return _fake_convert


def test_run_opendataloader_single_page(tmp_path: Path) -> None:
    pdf = tmp_path / "doc.pdf"
    pdf.write_bytes(b"%PDF-1.4")
    md_body = "# Vertrag\n\n§ 1 Beispiel"
    tmp_dir = tmp_path / "work"

    with patch("opendataloader_pdf.convert", _fake_convert_factory(md_body, "doc")):
        result = run_opendataloader(pdf, tmp_dir)

    assert result.page_count == 1
    assert result.pages_ok == 1
    assert result.pages_failed == 0
    assert result.pages[0].strategy == "opendataloader"
    assert "§ 1 Beispiel" in result.pages[0].text


def test_run_opendataloader_splits_pages_on_sentinel(tmp_path: Path) -> None:
    pdf = tmp_path / "doc.pdf"
    pdf.write_bytes(b"%PDF-1.4")
    md_body = f"# Seite eins{_PAGE_SEPARATOR}# Seite zwei{_PAGE_SEPARATOR}# Seite drei"
    tmp_dir = tmp_path / "work"

    with patch("opendataloader_pdf.convert", _fake_convert_factory(md_body, "doc")):
        result = run_opendataloader(pdf, tmp_dir)

    assert result.page_count == 3
    assert result.pages_ok == 3
    assert result.pages[0].text.startswith("# Seite eins")
    assert result.pages[1].text.startswith("# Seite zwei")
    assert result.pages[2].text.startswith("# Seite drei")


def test_run_opendataloader_writes_clean_output_path(tmp_path: Path) -> None:
    """Output path must not contain our internal page sentinel."""
    pdf = tmp_path / "doc.pdf"
    pdf.write_bytes(b"%PDF-1.4")
    md_body = f"page one{_PAGE_SEPARATOR}page two"
    tmp_dir = tmp_path / "work"
    output_path = tmp_path / "out" / "result.md"

    with patch("opendataloader_pdf.convert", _fake_convert_factory(md_body, "doc")):
        run_opendataloader(pdf, tmp_dir, output_path=output_path)

    written = output_path.read_text(encoding="utf-8")
    assert _PAGE_SEPARATOR not in written
    assert "page one" in written and "page two" in written


def test_run_opendataloader_marks_empty_pages_as_failed(tmp_path: Path) -> None:
    pdf = tmp_path / "doc.pdf"
    pdf.write_bytes(b"%PDF-1.4")
    # Second "page" is empty
    md_body = f"real content{_PAGE_SEPARATOR}{_PAGE_SEPARATOR}also real"
    tmp_dir = tmp_path / "work"

    with patch("opendataloader_pdf.convert", _fake_convert_factory(md_body, "doc")):
        result = run_opendataloader(pdf, tmp_dir)

    assert result.page_count == 3
    assert result.pages_ok == 2
    assert result.pages_failed == 1
    assert result.pages[1].text is None


def test_run_opendataloader_raises_when_no_output(tmp_path: Path) -> None:
    pdf = tmp_path / "doc.pdf"
    pdf.write_bytes(b"%PDF-1.4")
    tmp_dir = tmp_path / "work"

    def _fake_convert_nothing(*_args, **_kwargs):
        pass  # writes nothing

    with patch("opendataloader_pdf.convert", _fake_convert_nothing):
        try:
            run_opendataloader(pdf, tmp_dir)
        except RuntimeError as e:
            assert "no markdown output" in str(e).lower()
        else:
            raise AssertionError("expected RuntimeError")


# ── BBox-Anchor injection ────────────────────────────────────────────────


def test_inject_bbox_anchors_prepends_anchors_for_known_blocks(tmp_path: Path) -> None:
    from app.services.opendataloader_pipeline import _inject_bbox_anchors

    structure = {
        "kids": [
            {
                "type": "heading",
                "page number": 1,
                "bounding box": [88.0, 553.0, 295.7, 568.4],
                "content": "Wohnungseigentumsgesetz (WEG)",
            },
            {
                "type": "paragraph",
                "page number": 1,
                "bounding box": [68.1, 749.0, 207.2, 762.3],
                "content": "Vertragsbestandteil AZ120.8",
            },
        ]
    }
    structure_path = tmp_path / "structure.json"
    structure_path.write_text(__import__("json").dumps(structure))

    md = "# Wohnungseigentumsgesetz (WEG)\n\nVertragsbestandteil AZ120.8\n"
    out = _inject_bbox_anchors(md, structure_path)

    assert 'id="odl-p1-bbox-88.0-553.0-295.7-568.4"' in out
    assert 'id="odl-p1-bbox-68.1-749.0-207.2-762.3"' in out
    # Original markdown must still be intact (anchors are prepended)
    assert "# Wohnungseigentumsgesetz (WEG)" in out
    assert "Vertragsbestandteil AZ120.8" in out
