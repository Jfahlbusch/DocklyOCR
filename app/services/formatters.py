"""Output formatters for ``OcrResult``.

Supported formats:

* ``md``   — Markdown with ``## Seite N`` headings + strategy footnote
* ``txt``  — Plain text, pages separated by form-feed (``\\f``)
* ``toon`` — Custom TOON format with ``page[N]:`` blocks + § section detection
* ``json`` — Structured JSON with ``meta`` + ``pages`` arrays

All formatters return ``(body_bytes, mime_type)`` so the caller (HTTP layer)
can stream them out without re-encoding.
"""

from __future__ import annotations

import json
import re

from app.services.ocr_pipeline import OcrResult, PageResult

# ── MIME types ────────────────────────────────────────────────────────

_MIME_MD = "text/markdown; charset=utf-8"
_MIME_TXT = "text/plain; charset=utf-8"
_MIME_TOON = "application/x-toon; charset=utf-8"  # no standard MIME for TOON
_MIME_JSON = "application/json; charset=utf-8"


# ── Markdown ──────────────────────────────────────────────────────────


def _format_md(result: OcrResult) -> bytes:
    lines: list[str] = []
    for page in result.pages:
        lines.append(f"## Seite {page.number}")
        lines.append("")
        if page.text is not None and page.text.strip():
            lines.append(page.text)
            lines.append("")
            if not page.is_table:
                lines.append(f"> OCR-Strategie: `{page.strategy}`")
        else:
            lines.append(f"[OCR-Fehler auf Seite {page.number} — alle Strategien fehlgeschlagen]")
        lines.append("")
    return "\n".join(lines).encode("utf-8")


# ── Plain text ────────────────────────────────────────────────────────


def _format_txt(result: OcrResult) -> bytes:
    parts: list[str] = []
    for page in result.pages:
        if page.text is not None and page.text.strip():
            parts.append(page.text)
        else:
            parts.append(f"[OCR-Fehler Seite {page.number}]")
    # Form-feed (\f, 0x0c) separates pages — including a trailing one,
    # matching common page-stream conventions.
    body = "\f".join(parts)
    if parts:
        body += "\f"
    return body.encode("utf-8")


# ── TOON (ported from Anhang C) ───────────────────────────────────────


def _format_toon(result: OcrResult, title: str = "document") -> bytes:
    """Port of ``text_to_toon`` from Anhang C, taking PageResult list directly."""
    lines: list[str] = []
    lines.append("document:")
    lines.append(f"  title: {title}")
    lines.append("  type: legal_document")
    lines.append(f"  pages: {result.page_count}")
    for page in result.pages:
        lines.append(f"page[{page.number}]:")
        if page.text is None:
            lines.append("  text: [OCR-Fehler]")
            continue

        text = page.text
        sections = re.split(r"(§\s*\d+)", text)
        if len(sections) > 1:
            i = 0
            while i < len(sections):
                part = sections[i].strip()
                if re.match(r"§\s*\d+", part) and i + 1 < len(sections):
                    sec_title = part
                    sec_text = sections[i + 1].strip()[:200]
                    lines.append(f"  {sec_title}:")
                    lines.append(f"    title: {sec_title}")
                    lines.append(f"    text: {sec_text}")
                    i += 2
                else:
                    if part:
                        lines.append(f"  text: {part[:300]}")
                    i += 1
        else:
            lines.append(f"  text: {text[:300]}")
    return "\n".join(lines).encode("utf-8")


# ── JSON ──────────────────────────────────────────────────────────────


def _format_json(result: OcrResult) -> bytes:
    payload = {
        "meta": {
            "page_count": result.page_count,
            "pages_ok": result.pages_ok,
            "pages_failed": result.pages_failed,
        },
        "pages": [
            {
                "number": p.number,
                "text": p.text,
                "strategy": p.strategy,
                "elapsed_s": p.elapsed_s,
                "is_table": p.is_table,
            }
            for p in result.pages
        ],
    }
    return json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8")


# ── Public dispatch ───────────────────────────────────────────────────


def format_output(result: OcrResult, fmt: str) -> tuple[bytes, str]:
    """Render ``result`` in the requested format.

    Returns ``(body_bytes, mime_type)``. Raises ``ValueError`` for unknown
    format strings.
    """
    if fmt == "md":
        return _format_md(result), _MIME_MD
    if fmt == "txt":
        return _format_txt(result), _MIME_TXT
    if fmt == "toon":
        return _format_toon(result), _MIME_TOON
    if fmt == "json":
        return _format_json(result), _MIME_JSON
    raise ValueError(f"Unknown output format: {fmt}")


__all__ = ["format_output", "OcrResult", "PageResult"]
