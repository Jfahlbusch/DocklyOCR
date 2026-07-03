"""Tests for the deterministic value extraction (entities.json sidecar)."""

from __future__ import annotations

import json
from pathlib import Path

from app.services.entity_extractor import extract_entities
from app.services.ocr_pipeline import OcrResult, PageResult


def _result_from_text(text: str, page: int = 1) -> OcrResult:
    pages = [PageResult(number=page, text=text, strategy="opendataloader", elapsed_s=0.1)]
    return OcrResult(pages=pages, page_count=1, pages_ok=1, pages_failed=0)


# ── Amounts ──────────────────────────────────────────────────────────────


def test_amount_german_full_form() -> None:
    e = extract_entities(_result_from_text("Versicherungssumme: 1.500.000,00 EUR je Schadenfall"))
    assert len(e["amounts"]) == 1
    a = e["amounts"][0]
    assert a["value"] == 1500000.00
    assert a["currency"] == "EUR"
    assert a["raw"] == "1.500.000,00 EUR"
    assert "Versicherungssumme" in a["context"]


def test_amount_euro_sign_suffix() -> None:
    e = extract_entities(_result_from_text("Selbstbeteiligung 500 € je Fall"))
    assert e["amounts"][0]["value"] == 500.0


def test_amount_comma_dash_notation() -> None:
    """',--' is the classic insurance notation for ,00."""
    e = extract_entities(_result_from_text("Beitrag: 2.345,-- EUR"))
    assert e["amounts"][0]["value"] == 2345.0


def test_amount_currency_prefix() -> None:
    e = extract_entities(_result_from_text("EUR 12.500,50 pro Jahr"))
    assert e["amounts"][0]["value"] == 12500.50


def test_amount_millions_shortform() -> None:
    e = extract_entities(_result_from_text("Deckungssumme 1,5 Mio. EUR pauschal"))
    assert e["amounts"][0]["value"] == 1_500_000.0


def test_amount_teur() -> None:
    e = extract_entities(_result_from_text("Limit 500 T€ je Position"))
    assert e["amounts"][0]["value"] == 500_000.0


def test_bare_number_is_not_an_amount() -> None:
    """Numbers without currency markers must NOT be classified as amounts."""
    e = extract_entities(_result_from_text("Auf Seite 1500 stehen 42 Positionen."))
    assert e["amounts"] == []


# ── Percentages ──────────────────────────────────────────────────────────


def test_percentage_simple_and_decimal() -> None:
    e = extract_entities(_result_from_text("Mitversicherung 20 % bzw. 12,5% Anteil"))
    values = sorted(p["value"] for p in e["percentages"])
    assert values == [12.5, 20.0]


# ── Dates ────────────────────────────────────────────────────────────────


def test_date_normalised_to_iso() -> None:
    e = extract_entities(_result_from_text("Vertragsbeginn: 01.05.2026, Ablauf 1.5.27"))
    isos = sorted(d["iso"] for d in e["dates"])
    assert isos == ["2026-05-01", "2027-05-01"]


def test_invalid_date_rejected() -> None:
    e = extract_entities(_result_from_text("Kennziffer 99.99.2026 ist keine Angabe"))
    assert e["dates"] == []


# ── Policy numbers ───────────────────────────────────────────────────────


def test_policy_number_with_label() -> None:
    e = extract_entities(_result_from_text("Versicherungsschein-Nr.: AB-123456/78 vom 01.01.2026"))
    assert len(e["policy_numbers"]) == 1
    p = e["policy_numbers"][0]
    assert p["raw"] == "AB-123456/78"
    assert "Versicherungsschein" in p["label"]


def test_vertragsnummer_label() -> None:
    e = extract_entities(_result_from_text("Vertragsnummer: 4711.0815"))
    assert e["policy_numbers"][0]["raw"] == "4711.0815"


def test_digit_run_without_label_not_captured() -> None:
    e = extract_entities(_result_from_text("Es gelten die Ziffern 123456 der AVB."))
    assert e["policy_numbers"] == []


# ── Dedupe + paging ──────────────────────────────────────────────────────


def test_same_value_same_page_deduped() -> None:
    e = extract_entities(_result_from_text("SB 500 EUR. Es gilt: SB 500 EUR."))
    assert len(e["amounts"]) == 1


def test_same_value_different_pages_kept() -> None:
    pages = [
        PageResult(number=1, text="SB 500 EUR", strategy="x", elapsed_s=0.1),
        PageResult(number=2, text="SB 500 EUR", strategy="x", elapsed_s=0.1),
    ]
    result = OcrResult(pages=pages, page_count=2, pages_ok=2, pages_failed=0)
    e = extract_entities(result)
    assert len(e["amounts"]) == 2
    assert {a["page"] for a in e["amounts"]} == {1, 2}


def test_meta_counts() -> None:
    e = extract_entities(_result_from_text("500 EUR und 20 % ab 01.01.2026"))
    assert e["meta"]["counts"]["amounts"] == 1
    assert e["meta"]["counts"]["percentages"] == 1
    assert e["meta"]["counts"]["dates"] == 1


# ── BBox enrichment (opendataloader) ─────────────────────────────────────


def test_bbox_enrichment_from_structure(tmp_path: Path) -> None:
    structure = {
        "kids": [
            {
                "type": "paragraph",
                "page number": 3,
                "bounding box": [100.0, 200.0, 300.0, 220.0],
                "content": "Versicherungssumme: 1.500.000,00 EUR je Schadenfall",
            }
        ]
    }
    sp = tmp_path / "structure.json"
    sp.write_text(json.dumps(structure))

    e = extract_entities(
        _result_from_text("Versicherungssumme: 1.500.000,00 EUR je Schadenfall", page=3),
        structure_path=sp,
    )
    a = e["amounts"][0]
    assert a["bbox"] == [100.0, 200.0, 300.0, 220.0]
    assert a["pdf_page"] == 3


def test_bbox_skipped_when_ambiguous(tmp_path: Path) -> None:
    """Same raw string in two elements → no bbox (ambiguous)."""
    structure = {
        "kids": [
            {"page number": 1, "bounding box": [1, 2, 3, 4], "content": "SB 500 EUR hier"},
            {"page number": 2, "bounding box": [5, 6, 7, 8], "content": "SB 500 EUR dort"},
        ]
    }
    sp = tmp_path / "structure.json"
    sp.write_text(json.dumps(structure))

    e = extract_entities(_result_from_text("SB 500 EUR"), structure_path=sp)
    assert "bbox" not in e["amounts"][0]
