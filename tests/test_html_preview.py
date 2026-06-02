"""Tests for the Markdown → HTML preview builder used by vllm jobs."""

from __future__ import annotations

from app.services.html_preview import build_preview_from_markdown


def test_build_preview_wraps_in_html_document() -> None:
    out = build_preview_from_markdown("# Hello\n\nWorld")
    assert "<!DOCTYPE html>" in out
    assert "<title>OCR Preview</title>" in out
    assert "<h1>Hello</h1>" in out
    assert "<p>World</p>" in out


def test_build_preview_renders_tables() -> None:
    md = """| col A | col B |
|-------|-------|
| 1     | foo   |
| 2     | bar   |
"""
    out = build_preview_from_markdown(md)
    assert "<table>" in out
    assert "<th>col A</th>" in out
    assert "<td>foo</td>" in out


def test_build_preview_includes_meta_when_provided() -> None:
    out = build_preview_from_markdown(
        "body",
        title="my-job.pdf",
        meta={"Engine": "vllm", "Seiten": "3/3"},
    )
    assert "<title>my-job.pdf</title>" in out
    assert "Engine" in out and "vllm" in out
    assert "Seiten" in out and "3/3" in out


def test_build_preview_html_escapes_meta_values() -> None:
    out = build_preview_from_markdown("body", meta={"Engine": "<script>x</script>"})
    assert "<script>" not in out
    assert "&lt;script&gt;" in out


def test_build_preview_omits_meta_block_when_empty() -> None:
    out = build_preview_from_markdown("body")
    assert 'class="ocr-meta"' not in out
