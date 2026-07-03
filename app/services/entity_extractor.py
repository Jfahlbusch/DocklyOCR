"""Deterministic extraction of typed values from OCR results.

Runs after the pipeline (both engines) and produces the ``entities.json``
sidecar: every money amount, percentage, date and policy number found in
the extracted text, normalised into machine-readable form with page
number and a context snippet.

Why regex and not an LLM: values in insurance documents are the one
thing that must never be hallucinated. A regex can only find what is
literally in the text. Downstream consumers (DocklyProtect, the
Gutachter pipeline) get canonical floats/ISO dates instead of having to
re-parse German number formats themselves — eliminating the classic
"1.500" (de: 1500) vs "1.500" (en: 1.5) factor-1000 mistake.

The full pattern catalogue is documented in ``docs/WERTERKENNUNG.md``.
"""

from __future__ import annotations

import json
import logging
import re
from pathlib import Path
from typing import Any

from app.services.ocr_pipeline import OcrResult

logger = logging.getLogger(__name__)

_CONTEXT_CHARS = 60  # snippet radius around each match

# ── Amounts ──────────────────────────────────────────────────────────────
# German notation: "." groups thousands, "," starts decimals. A currency
# marker (before or after) is REQUIRED — a bare number is not an amount.
# ",--" and ",-" are treated as ",00" (common in insurance documents).

# 1.500.000,00 EUR | 500,-- € | 1.500 EUR | 500 T€ | 2 TEUR
_AMOUNT_SUFFIX_RE = re.compile(
    r"(?<![\d.,])"
    r"(?P<int>\d{1,3}(?:\.\d{3})+|\d+)"
    r"(?:,(?P<dec>\d{1,2}|--|-))?"
    r"\s?(?P<cur>T?EUR|T?€)(?![A-Za-z])"
)

# EUR 1.500.000,00 | € 500,--
_AMOUNT_PREFIX_RE = re.compile(
    r"(?P<cur>EUR|€)\s?"
    r"(?P<int>\d{1,3}(?:\.\d{3})+|\d+)"
    r"(?:,(?P<dec>\d{1,2}|--|-))?"
    r"(?![\d.,]\d)"
)

# 1,5 Mio. EUR | 2 Mio € | 1,25 Mio.
_AMOUNT_MIO_RE = re.compile(
    r"(?<![\d.,])"
    r"(?P<num>\d{1,4}(?:,\d{1,2})?)"
    r"\s?(?P<scale>Mio|Mrd)\.?"
    r"\s?(?:EUR|€)?(?![A-Za-z])"
)

# ── Percentages ──────────────────────────────────────────────────────────
_PERCENT_RE = re.compile(r"(?<![\d.,])(?P<num>\d{1,3}(?:,\d{1,2})?)\s?%")

# ── Dates ────────────────────────────────────────────────────────────────
# 01.01.2026 | 1.1.26 — validated (month 1-12, day 1-31); 2-digit years
# are expanded to 20xx.
_DATE_RE = re.compile(r"\b(?P<d>\d{1,2})\.(?P<m>\d{1,2})\.(?P<y>\d{4}|\d{2})\b")

# ── Policy / contract numbers ────────────────────────────────────────────
# Label-anchored: only sequences directly following a recognisable label
# are captured, to avoid classifying arbitrary digit runs as policy IDs.
_POLICY_RE = re.compile(
    r"(?P<label>(?:Versicherungsschein|Vertrags|Policen?|Schein|Antrags)"
    r"[-\s]?(?:nummer|Nr\.?|No\.?))"
    r"\s*[:.]?\s*"
    r"(?P<num>[A-Z0-9][A-Z0-9\-./]{3,30})",
    re.IGNORECASE,
)


def _to_float_german(int_part: str, dec_part: str | None) -> float:
    """``1.500.000`` + ``50`` → 1500000.50; ``,--``/``,-`` counts as ,00."""
    value = float(int_part.replace(".", ""))
    if dec_part and dec_part not in ("--", "-"):
        value += float(f"0.{dec_part}")
    return value


def _context(text: str, start: int, end: int) -> str:
    lo = max(0, start - _CONTEXT_CHARS)
    hi = min(len(text), end + _CONTEXT_CHARS)
    snippet = " ".join(text[lo:hi].split())
    return snippet


def _extract_amounts(text: str, page: int) -> list[dict[str, Any]]:
    found: list[dict[str, Any]] = []
    spans: list[tuple[int, int]] = []

    for m in _AMOUNT_MIO_RE.finditer(text):
        num = float(m.group("num").replace(",", "."))
        multiplier = 1_000_000 if m.group("scale") == "Mio" else 1_000_000_000
        found.append(
            {
                "raw": m.group(0).strip(),
                "value": num * multiplier,
                "currency": "EUR",
                "page": page,
                "context": _context(text, m.start(), m.end()),
            }
        )
        spans.append(m.span())

    for regex in (_AMOUNT_SUFFIX_RE, _AMOUNT_PREFIX_RE):
        for m in regex.finditer(text):
            # skip if this span overlaps an already-captured Mio match
            if any(s < m.end() and m.start() < e for s, e in spans):
                continue
            cur = m.group("cur").upper().replace("€", "EUR")
            multiplier = 1_000 if cur in ("TEUR", "T€", "TEUR") else 1
            if cur.startswith("T") and cur != "EUR":
                cur = "EUR"
                multiplier = 1_000
            value = _to_float_german(m.group("int"), m.group("dec")) * multiplier
            found.append(
                {
                    "raw": m.group(0).strip(),
                    "value": value,
                    "currency": "EUR",
                    "page": page,
                    "context": _context(text, m.start(), m.end()),
                }
            )
            spans.append(m.span())
    return found


def _extract_percentages(text: str, page: int) -> list[dict[str, Any]]:
    return [
        {
            "raw": m.group(0).strip(),
            "value": float(m.group("num").replace(",", ".")),
            "page": page,
            "context": _context(text, m.start(), m.end()),
        }
        for m in _PERCENT_RE.finditer(text)
    ]


def _extract_dates(text: str, page: int) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for m in _DATE_RE.finditer(text):
        day, month = int(m.group("d")), int(m.group("m"))
        year_raw = m.group("y")
        if not (1 <= day <= 31 and 1 <= month <= 12):
            continue
        year = int(year_raw) if len(year_raw) == 4 else 2000 + int(year_raw)
        out.append(
            {
                "raw": m.group(0),
                "iso": f"{year:04d}-{month:02d}-{day:02d}",
                "page": page,
                "context": _context(text, m.start(), m.end()),
            }
        )
    return out


def _extract_policy_numbers(text: str, page: int) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for m in _POLICY_RE.finditer(text):
        num = m.group("num").rstrip(".,")
        # must contain at least one digit — filters "Vertragsnummer siehe"
        if not any(c.isdigit() for c in num):
            continue
        out.append(
            {
                "raw": num,
                "label": " ".join(m.group("label").split()),
                "page": page,
                "context": _context(text, m.start(), m.end()),
            }
        )
    return out


def _walk_structure_elements(node: object, out: list[dict]) -> None:
    if isinstance(node, dict):
        if node.get("content") and node.get("bounding box"):
            out.append(node)
        for v in node.values():
            _walk_structure_elements(v, out)
    elif isinstance(node, list):
        for v in node:
            _walk_structure_elements(v, out)


def _enrich_with_bboxes(entities: dict[str, list[dict]], structure_path: Path) -> None:
    """Best-effort: attach ``bbox`` + ``pdf_page`` from the opendataloader
    structure sidecar to every entity whose raw text appears verbatim in
    exactly one element. Ambiguous or unmatched entities stay bbox-less."""
    try:
        data = json.loads(structure_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as e:
        logger.warning("structure.json unreadable for bbox enrichment: %s", e)
        return

    elements: list[dict] = []
    _walk_structure_elements(data, elements)

    for bucket in entities.values():
        if not isinstance(bucket, list):
            continue
        for ent in bucket:
            raw = ent.get("raw", "")
            matches = [el for el in elements if raw and raw in el.get("content", "")]
            if len(matches) == 1:
                el = matches[0]
                bbox = el["bounding box"]
                ent["bbox"] = [round(v, 1) for v in bbox]
                if "page number" in el:
                    ent["pdf_page"] = el["page number"]


def extract_entities(result: OcrResult, structure_path: Path | None = None) -> dict[str, Any]:
    """Extract all typed values from an ``OcrResult``.

    Args:
        result: The pipeline output (either engine).
        structure_path: Path to the opendataloader ``structure.json``
            when available — used to attach bounding boxes.

    Returns:
        JSON-serialisable dict with keys ``amounts``, ``percentages``,
        ``dates``, ``policy_numbers`` and a ``meta`` block.
    """
    entities: dict[str, Any] = {
        "amounts": [],
        "percentages": [],
        "dates": [],
        "policy_numbers": [],
    }

    for pageresult in result.pages:
        if not pageresult.text:
            continue
        page = pageresult.number
        entities["amounts"] += _extract_amounts(pageresult.text, page)
        entities["percentages"] += _extract_percentages(pageresult.text, page)
        entities["dates"] += _extract_dates(pageresult.text, page)
        entities["policy_numbers"] += _extract_policy_numbers(pageresult.text, page)

    # Dedupe: identical (raw, page) pairs appear when a value is repeated
    # inside the same page (e.g. table + footnote) — keep first occurrence.
    for key, bucket in entities.items():
        seen: set[tuple] = set()
        unique = []
        for ent in bucket:
            fingerprint = (ent["raw"], ent["page"])
            if fingerprint in seen:
                continue
            seen.add(fingerprint)
            unique.append(ent)
        entities[key] = unique

    if structure_path is not None and structure_path.exists():
        _enrich_with_bboxes(entities, structure_path)

    entities["meta"] = {
        "counts": {k: len(v) for k, v in entities.items() if isinstance(v, list)},
        "extractor_version": 1,
    }
    return entities
