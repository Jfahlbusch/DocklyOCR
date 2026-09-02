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


# ── Categories ───────────────────────────────────────────────────────────
# Signal words that classify what a number actually *means*. Matched
# against the text immediately BEFORE the value (German syntax puts the
# label first: "Selbstbeteiligung: 500 EUR"), falling back to the text
# after it. Order matters — the first matching category wins, so more
# specific terms must come first.

_AMOUNT_CATEGORIES: list[tuple[str, tuple[str, ...]]] = [
    (
        "selbstbehalt",
        ("selbstbeteiligung", "selbstbehalt", "eigenanteil", " sb ", "sb:", "abzugsfranchise"),
    ),
    (
        "praemie",
        (
            "beitrag",
            "prämie",
            "praemie",
            "versicherungsteuer",
            "zahlweise",
            "nettobeitrag",
            "bruttobeitrag",
        ),
    ),
    (
        "sublimit",
        (
            "sublimit",
            "höchstersatzleistung",
            "hoechstersatzleistung",
            "entschädigungsgrenze",
            "entschaedigungsgrenze",
            "begrenzt auf",
            "erstrisikosumme",
            "erstes risiko",
            "maximal je",
            "max. je",
            "jahreshöchstleistung",
        ),
    ),
    (
        "versicherungssumme",
        (
            "versicherungssumme",
            "deckungssumme",
            "versicherungswert",
            "haftungssumme",
            "pauschalsumme",
            "versichert mit",
        ),
    ),
    (
        "bemessungsgrundlage",
        ("umsatz", "lohnsumme", "bausumme", "wertermittlung", "bemessungsgrundlage", "mietwert"),
    ),
]

_DATE_CATEGORIES: list[tuple[str, tuple[str, ...]]] = [
    (
        "vertragsbeginn",
        ("versicherungsbeginn", "vertragsbeginn", "beginn", "beginnt am", "wirksam ab"),
    ),
    (
        "vertragsablauf",
        ("ablauf", "vertragsende", "endet am", "hauptfälligkeit", "hauptfaelligkeit", "befristet"),
    ),
    ("stichtag", ("stichtag", "bewertungsstichtag", "zum stand", "nachweispflicht", "frist bis")),
]

# ── References: bedingungswerke + Rechtsnormen ───────────────────────────
# Standard German insurance condition sets. Matched as whole words with an
# optional year, e.g. "AFB 2008", "AHB", "AVB-Cyber". The catalogue mirrors
# the clause-to-phase mapping table in Plan_OpenSource_Gutachter_KI.md.
_BEDINGUNGSWERKE = (
    "AFB",
    "AERB",
    "AWB",
    "ASTB",
    "MFBU",
    "ABE",
    "ABMG",
    "ABN",
    "ABU",
    "AHB",
    "BHV",
    "AVB-PV",
    "AVB-Cyber",
    "AVB-WG",
    "KFV",
    "ULLA",
    "D&O",
    "VHB",
    "VGB",
    "AVBR",
    "BBR",
    "AMB",
    "AStB",
    "ARB",
    "AKB",
)
_BEDINGUNGSWERK_RE = re.compile(
    r"(?<![A-Za-z0-9-])(?P<code>"
    + "|".join(re.escape(c) for c in sorted(_BEDINGUNGSWERKE, key=len, reverse=True))
    + r")(?![A-Za-z0-9])(?:\s?(?P<year>(?:19|20)\d{2}))?"
)

# "§ 19 VVG", "§§ 19, 20 VVG", "§ 823 Abs. 1 BGB", "Art. 5 DSGVO"
_GESETZE = (
    "VVG",
    "BGB",
    "HGB",
    "AktG",
    "GmbHG",
    "SGB",
    "ZPO",
    "StGB",
    "WEG",
    "VAG",
    "AWG",
    "AWV",
    "DSGVO",
    "ProdHaftG",
    "UStG",
    "EStG",
    "InsO",
    "GewO",
    "BImSchG",
    "WHG",
    "ArbSchG",
    "StVG",
)
_NORM_RE = re.compile(
    r"(?P<sym>§{1,2}|Art\.)\s?(?P<num>\d{1,4}[a-z]?)"
    r"(?P<extra>(?:\s?(?:Abs\.|Absatz|Satz|Nr\.|Ziffer)\s?\d+[a-z]?)*)"
    r"(?:\s?(?:und|,)\s?\d{1,4}[a-z]?)*"
    r"\s?(?P<gesetz>" + "|".join(_GESETZE) + r")(?![A-Za-z])"
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


# A Markdown table separator row: |---|---|---| (with optional colons)
_TABLE_SEP_RE = re.compile(r"^\s*\|?\s*:?-{2,}:?\s*(?:\|\s*:?-{2,}:?\s*)*\|?\s*$")


def _table_column_label(text: str, pos: int) -> str | None:
    """If ``pos`` sits in a Markdown pipe-table data row, return that
    column's header cell.

    Rate tables are everywhere in insurance documents, and there the
    meaning of a number comes from its column header, not from the words
    next to it. Without this, "1.500.000,00 EUR" in the
    *Versicherungssumme* column gets mislabelled from whatever header
    happens to fall inside the character window.
    """
    line_start = text.rfind("\n", 0, pos) + 1
    line_end = text.find("\n", pos)
    if line_end < 0:
        line_end = len(text)
    line = text[line_start:line_end]
    if line.count("|") < 2:
        return None

    column = line[: pos - line_start].count("|") - 1
    if column < 0:
        return None

    # Walk upwards to the separator row; the header is the line above it.
    lines_above = text[:line_start].splitlines()
    for i in range(len(lines_above) - 1, -1, -1):
        candidate = lines_above[i]
        if _TABLE_SEP_RE.match(candidate):
            if i == 0:
                return None
            cells = [c.strip() for c in lines_above[i - 1].strip().strip("|").split("|")]
            return cells[column] if 0 <= column < len(cells) else None
        if "|" not in candidate:
            return None  # left the table before finding a header
    return None


def _match_catalogue(window: str, catalogue: list[tuple[str, tuple[str, ...]]]) -> str:
    """First matching category in catalogue order, or ``"unbekannt"``."""
    for category, needles in catalogue:
        if any(n in window for n in needles):
            return category
    return "unbekannt"


def _categorize(
    text: str,
    start: int,
    end: int,
    catalogue: list[tuple[str, tuple[str, ...]]],
) -> str:
    """Classify a value by the signal words around it.

    The text BEFORE the value is checked first — German writes the label
    ahead of the number ("Selbstbeteiligung: 500 EUR"). Only if nothing
    matches there do we look at the text after it.

    Within a window the *nearest* signal word wins, not the first one in
    the catalogue. That matters because the context window routinely spans
    several values: in "Vertragsbeginn: 01.07.2026, Ablauf: 01.07.2027"
    both labels precede the second date, and only proximity tells us that
    it belongs to "Ablauf".

    Inside a Markdown table the column header decides instead — see
    :func:`_table_column_label`.

    Returns ``"unbekannt"`` when no signal word is found; we never guess.
    """
    header = _table_column_label(text, start)
    if header:
        from_header = _match_catalogue(f" {header.lower()} ", catalogue)
        if from_header != "unbekannt":
            return from_header
        # In a table but the header says nothing useful — the surrounding
        # cells would only mislead, so stop here.
        return "unbekannt"

    before = " " + " ".join(text[max(0, start - _CONTEXT_CHARS) : start].split()).lower() + " "
    after = " " + " ".join(text[end : end + _CONTEXT_CHARS].split()).lower() + " "

    # In `before` the closest signal word is the LAST occurrence; in
    # `after` it is the first. Score by distance to the value.
    for window, nearest in ((before, "last"), (after, "first")):
        best_category, best_distance = "unbekannt", None
        for category, needles in catalogue:
            for needle in needles:
                pos = window.rfind(needle) if nearest == "last" else window.find(needle)
                if pos < 0:
                    continue
                distance = len(window) - pos if nearest == "last" else pos
                if best_distance is None or distance < best_distance:
                    best_category, best_distance = category, distance
        if best_distance is not None:
            return best_category
    return "unbekannt"


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
                "category": _categorize(text, m.start(), m.end(), _AMOUNT_CATEGORIES),
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
                    "category": _categorize(text, m.start(), m.end(), _AMOUNT_CATEGORIES),
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
                "category": _categorize(text, m.start(), m.end(), _DATE_CATEGORIES),
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


def _extract_references(text: str, page: int) -> list[dict[str, Any]]:
    """Find condition sets (AFB 2008, AHB, …) and legal norms (§ 19 VVG).

    These feed the clause-to-phase mapping and the legal-citation
    whitelist of the Gutachter pipeline. Both are closed vocabularies, so
    matching is exact — no guessing about what a code might mean.
    """
    out: list[dict[str, Any]] = []

    for m in _BEDINGUNGSWERK_RE.finditer(text):
        entry: dict[str, Any] = {
            "raw": " ".join(m.group(0).split()),
            "type": "bedingungswerk",
            "code": m.group("code"),
            "page": page,
            "context": _context(text, m.start(), m.end()),
        }
        if m.group("year"):
            entry["year"] = int(m.group("year"))
        out.append(entry)

    for m in _NORM_RE.finditer(text):
        out.append(
            {
                "raw": " ".join(m.group(0).split()),
                "type": "rechtsnorm",
                "gesetz": m.group("gesetz"),
                "paragraph": m.group("num"),
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
        ``dates``, ``policy_numbers``, ``references`` and a ``meta`` block.
    """
    entities: dict[str, Any] = {
        "amounts": [],
        "percentages": [],
        "dates": [],
        "policy_numbers": [],
        "references": [],
    }

    for pageresult in result.pages:
        if not pageresult.text:
            continue
        page = pageresult.number
        entities["amounts"] += _extract_amounts(pageresult.text, page)
        entities["percentages"] += _extract_percentages(pageresult.text, page)
        entities["dates"] += _extract_dates(pageresult.text, page)
        entities["policy_numbers"] += _extract_policy_numbers(pageresult.text, page)
        entities["references"] += _extract_references(pageresult.text, page)

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
        # Rolled-up view so consumers can answer "which condition sets
        # apply?" / "how are the amounts distributed?" without iterating.
        "amount_categories": _tally(entities["amounts"], "category"),
        "date_categories": _tally(entities["dates"], "category"),
        "bedingungswerke": sorted(
            {r["code"] for r in entities["references"] if r["type"] == "bedingungswerk"}
        ),
        "rechtsnormen": sorted(
            {r["raw"] for r in entities["references"] if r["type"] == "rechtsnorm"}
        ),
        "extractor_version": 2,
    }
    return entities


def _tally(bucket: list[dict[str, Any]], key: str) -> dict[str, int]:
    """Count occurrences of ``key`` values, most frequent first."""
    counts: dict[str, int] = {}
    for item in bucket:
        v = item.get(key, "unbekannt")
        counts[v] = counts.get(v, 0) + 1
    return dict(sorted(counts.items(), key=lambda kv: (-kv[1], kv[0])))
