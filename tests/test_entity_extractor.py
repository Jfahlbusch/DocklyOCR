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


# ── Kategorien: Beträge ──────────────────────────────────────────────────


def test_amount_category_selbstbehalt() -> None:
    e = extract_entities(_result_from_text("Selbstbeteiligung: 500 EUR je Schadenfall"))
    assert e["amounts"][0]["category"] == "selbstbehalt"


def test_amount_category_versicherungssumme() -> None:
    e = extract_entities(_result_from_text("Die Versicherungssumme beträgt 1.500.000,00 EUR"))
    assert e["amounts"][0]["category"] == "versicherungssumme"


def test_amount_category_praemie() -> None:
    e = extract_entities(_result_from_text("Der Jahresbeitrag beläuft sich auf 2.345,-- EUR"))
    assert e["amounts"][0]["category"] == "praemie"


def test_amount_category_sublimit() -> None:
    e = extract_entities(_result_from_text("Sublimit: 300.000 EUR je Position"))
    assert e["amounts"][0]["category"] == "sublimit"


def test_amount_category_bemessungsgrundlage() -> None:
    e = extract_entities(_result_from_text("Jahresumsatz 4.000.000 EUR laut Meldebogen"))
    assert e["amounts"][0]["category"] == "bemessungsgrundlage"


def test_amount_category_unknown_when_no_signal() -> None:
    """No signal word → 'unbekannt'. We never guess."""
    e = extract_entities(_result_from_text("Es wurden 750 EUR erwähnt."))
    assert e["amounts"][0]["category"] == "unbekannt"


def test_amount_category_label_before_wins_over_after() -> None:
    """German puts the label first — the word BEFORE the value decides."""
    text = "Selbstbeteiligung 500 EUR, die Versicherungssumme folgt danach"
    e = extract_entities(_result_from_text(text))
    assert e["amounts"][0]["category"] == "selbstbehalt"


def test_amount_categories_tallied_in_meta() -> None:
    text = "Versicherungssumme 1.000.000 EUR. Selbstbeteiligung 500 EUR. Beitrag 900 EUR."
    e = extract_entities(_result_from_text(text))
    tally = e["meta"]["amount_categories"]
    assert tally["versicherungssumme"] == 1
    assert tally["selbstbehalt"] == 1
    assert tally["praemie"] == 1


# ── Kategorien: Daten ────────────────────────────────────────────────────


def test_date_category_beginn_und_ablauf() -> None:
    e = extract_entities(_result_from_text("Vertragsbeginn: 01.07.2026, Ablauf: 01.07.2027"))
    cats = {d["iso"]: d["category"] for d in e["dates"]}
    assert cats["2026-07-01"] == "vertragsbeginn"
    assert cats["2027-07-01"] == "vertragsablauf"


def test_date_category_unknown() -> None:
    e = extract_entities(_result_from_text("Irgendwann am 15.03.2026 passierte etwas."))
    assert e["dates"][0]["category"] == "unbekannt"


# ── References: Bedingungswerke ──────────────────────────────────────────


def test_reference_bedingungswerk_with_year() -> None:
    e = extract_entities(_result_from_text("Es gelten die AFB 2008 in der aktuellen Fassung."))
    refs = [r for r in e["references"] if r["type"] == "bedingungswerk"]
    assert len(refs) == 1
    assert refs[0]["code"] == "AFB"
    assert refs[0]["year"] == 2008
    assert refs[0]["raw"] == "AFB 2008"


def test_reference_bedingungswerk_without_year() -> None:
    e = extract_entities(_result_from_text("Grundlage sind die AHB sowie ergänzende Klauseln."))
    refs = [r for r in e["references"] if r["type"] == "bedingungswerk"]
    assert refs[0]["code"] == "AHB"
    assert "year" not in refs[0]


def test_reference_hyphenated_codes() -> None:
    e = extract_entities(_result_from_text("Zusätzlich vereinbart: AVB-Cyber und AVB-PV 2021."))
    codes = {r["code"] for r in e["references"] if r["type"] == "bedingungswerk"}
    assert "AVB-Cyber" in codes
    assert "AVB-PV" in codes


def test_reference_not_matched_inside_word() -> None:
    """'AFBX' or 'KAHB' must not produce a false positive."""
    e = extract_entities(_result_from_text("Der Code AFBX und das Kuerzel KAHB sind irrelevant."))
    assert [r for r in e["references"] if r["type"] == "bedingungswerk"] == []


# ── References: Rechtsnormen ─────────────────────────────────────────────


def test_reference_rechtsnorm_simple() -> None:
    e = extract_entities(_result_from_text("Anzeigepflicht nach § 19 VVG bei Gefahrerhöhung."))
    norms = [r for r in e["references"] if r["type"] == "rechtsnorm"]
    assert norms[0]["gesetz"] == "VVG"
    assert norms[0]["paragraph"] == "19"


def test_reference_rechtsnorm_with_absatz() -> None:
    e = extract_entities(_result_from_text("Haftung gemäß § 823 Abs. 1 BGB bleibt unberührt."))
    norms = [r for r in e["references"] if r["type"] == "rechtsnorm"]
    assert norms[0]["gesetz"] == "BGB"
    assert norms[0]["paragraph"] == "823"


def test_reference_rechtsnorm_artikel() -> None:
    e = extract_entities(_result_from_text("Verarbeitung nach Art. 6 DSGVO."))
    norms = [r for r in e["references"] if r["type"] == "rechtsnorm"]
    assert norms[0]["gesetz"] == "DSGVO"
    assert norms[0]["paragraph"] == "6"


def test_reference_meta_rollup() -> None:
    text = "Es gelten AFB 2008 und AHB. Anzeigepflicht nach § 19 VVG, ferner § 75 VVG."
    e = extract_entities(_result_from_text(text))
    assert e["meta"]["bedingungswerke"] == ["AFB", "AHB"]
    assert "§ 19 VVG" in e["meta"]["rechtsnormen"]
    assert "§ 75 VVG" in e["meta"]["rechtsnormen"]


def test_extractor_version_bumped_to_2() -> None:
    e = extract_entities(_result_from_text("nichts besonderes"))
    assert e["meta"]["extractor_version"] == 2


# ── Kategorien in Tabellen (Spaltenheader entscheidet) ───────────────────

_TABLE = """|Position|Versicherungssumme|Selbstbehalt|Anteil|
|---|---|---|---|
|Feuer|1.500.000,00 EUR|500 EUR|20 %|
|Leitungswasser|300.000 EUR|250 EUR|10 %|
"""


def test_table_column_header_decides_category() -> None:
    e = extract_entities(_result_from_text(_TABLE))
    by_raw = {a["raw"]: a["category"] for a in e["amounts"]}
    assert by_raw["1.500.000,00 EUR"] == "versicherungssumme"
    assert by_raw["300.000 EUR"] == "versicherungssumme"
    assert by_raw["500 EUR"] == "selbstbehalt"
    assert by_raw["250 EUR"] == "selbstbehalt"


def test_table_without_meaningful_header_stays_unknown() -> None:
    """Neighbouring cells must not leak in when the header says nothing."""
    table = "|Pos|Wert A|Wert B|\n|---|---|---|\n|X|900 EUR|800 EUR|\n"
    e = extract_entities(_result_from_text(table))
    assert all(a["category"] == "unbekannt" for a in e["amounts"])


def test_prose_after_table_still_uses_proximity() -> None:
    text = _TABLE + "\nDie Selbstbeteiligung beträgt zusätzlich 750 EUR.\n"
    e = extract_entities(_result_from_text(text))
    by_raw = {a["raw"]: a["category"] for a in e["amounts"]}
    assert by_raw["750 EUR"] == "selbstbehalt"
    assert by_raw["1.500.000,00 EUR"] == "versicherungssumme"


# ── Kategorien aus der JSON-Tabellenstruktur ─────────────────────────────


def _table_structure(*, header_span: int = 1) -> dict:
    """opendataloader-artige Tabellenstruktur: 1 Headerzeile + 1 Datenzeile."""

    def cell(row: int, col: int, text: str, cspan: int = 1) -> dict:
        return {
            "type": "table cell",
            "page number": 1,
            "bounding box": [10.0 * col, 100.0 - row, 10.0 * col + 8, 108.0 - row],
            "row number": row,
            "column number": col,
            "row span": 1,
            "column span": cspan,
            "kids": [{"type": "paragraph", "content": text}],
        }

    return {
        "kids": [
            {
                "type": "table",
                "page number": 1,
                "number of rows": 2,
                "number of columns": 3,
                "rows": [
                    {
                        "type": "table row",
                        "row number": 1,
                        "cells": [
                            cell(1, 1, "Position"),
                            cell(1, 2, "Versicherungssumme", cspan=header_span),
                            cell(1, 3, "Selbstbehalt")
                            if header_span == 1
                            else cell(1, 4, "Anteil"),
                        ],
                    },
                    {
                        "type": "table row",
                        "row number": 2,
                        "cells": [
                            cell(2, 1, "Feuer"),
                            cell(2, 2, "1.500.000,00 EUR"),
                            cell(2, 3, "500 EUR"),
                        ],
                    },
                ],
            }
        ]
    }


def test_json_table_header_sets_category(tmp_path: Path) -> None:
    """Werte in Tabellenzellen bekommen die Kategorie ihres Spaltenheaders."""
    sp = tmp_path / "structure.json"
    sp.write_text(json.dumps(_table_structure()))
    # Markdown ohne Tabellensyntax — die Kategorie kann nur aus der JSON kommen
    text = "Feuer 1.500.000,00 EUR 500 EUR"
    e = extract_entities(_result_from_text(text), structure_path=sp)

    by_raw = {a["raw"]: a for a in e["amounts"]}
    assert by_raw["1.500.000,00 EUR"]["category"] == "versicherungssumme"
    assert by_raw["1.500.000,00 EUR"]["category_source"] == "table_header"
    assert by_raw["500 EUR"]["category"] == "selbstbehalt"
    assert by_raw["500 EUR"]["category_source"] == "table_header"


def test_json_table_overrides_markdown_category(tmp_path: Path) -> None:
    """Die JSON-Struktur ist maßgeblich und korrigiert die Markdown-Heuristik."""
    sp = tmp_path / "structure.json"
    sp.write_text(json.dumps(_table_structure()))
    # Im Fließtext stünde "Selbstbeteiligung" direkt vor dem Betrag …
    text = "Selbstbeteiligung 1.500.000,00 EUR"
    e = extract_entities(_result_from_text(text), structure_path=sp)
    a = e["amounts"][0]
    # … die Tabellenspalte sagt aber: Versicherungssumme.
    assert a["category"] == "versicherungssumme"
    assert a["category_source"] == "table_header"


def test_json_table_column_span_applies_header_to_both_columns(tmp_path: Path) -> None:
    """Ein Header mit column span gilt für alle überspannten Spalten."""
    sp = tmp_path / "structure.json"
    sp.write_text(json.dumps(_table_structure(header_span=2)))
    text = "Feuer 1.500.000,00 EUR 500 EUR"
    e = extract_entities(_result_from_text(text), structure_path=sp)
    by_raw = {a["raw"]: a["category"] for a in e["amounts"]}
    # Spalte 3 ist von "Versicherungssumme" (span=2) mit abgedeckt
    assert by_raw["500 EUR"] == "versicherungssumme"


def test_json_table_ambiguous_value_untouched(tmp_path: Path) -> None:
    """Kommt der Wert in mehreren Zellen vor, bleibt die Kategorie unberührt."""
    struct = _table_structure()
    rows = struct["kids"][0]["rows"]
    rows[1]["cells"][2]["kids"][0]["content"] = "1.500.000,00 EUR"  # Dublette
    sp = tmp_path / "structure.json"
    sp.write_text(json.dumps(struct))

    e = extract_entities(_result_from_text("Beitrag 1.500.000,00 EUR"), structure_path=sp)
    a = e["amounts"][0]
    assert a["category"] == "praemie"  # aus dem Fließtext, nicht überschrieben
    assert a["category_source"] == "proximity"


def test_prose_value_keeps_proximity_source(tmp_path: Path) -> None:
    """Werte außerhalb von Tabellen behalten die Proximity-Kategorie."""
    sp = tmp_path / "structure.json"
    sp.write_text(json.dumps(_table_structure()))
    text = "Feuer 1.500.000,00 EUR 500 EUR. Der Jahresbeitrag beträgt 2.345,-- EUR."
    e = extract_entities(_result_from_text(text), structure_path=sp)
    by_raw = {a["raw"]: a for a in e["amounts"]}
    assert by_raw["2.345,-- EUR"]["category"] == "praemie"
    assert by_raw["2.345,-- EUR"]["category_source"] == "proximity"


def test_category_source_none_when_unknown() -> None:
    e = extract_entities(_result_from_text("Es wurden 750 EUR erwähnt."))
    assert e["amounts"][0]["category"] == "unbekannt"
    assert e["amounts"][0]["category_source"] == "none"


def test_json_table_does_not_leak_across_pages(tmp_path: Path) -> None:
    """Derselbe Betrag in Tabelle (S.1) und Fließtext (S.2): nur der
    Tabellenwert bekommt die Header-Kategorie."""
    sp = tmp_path / "structure.json"
    sp.write_text(json.dumps(_table_structure()))  # Tabelle liegt auf Seite 1

    pages = [
        PageResult(number=1, text="Feuer 1.500.000,00 EUR", strategy="x", elapsed_s=0.1),
        PageResult(number=2, text="Sublimit 1.500.000,00 EUR", strategy="x", elapsed_s=0.1),
    ]
    result = OcrResult(pages=pages, page_count=2, pages_ok=2, pages_failed=0)
    e = extract_entities(result, structure_path=sp)

    by_page = {a["page"]: a for a in e["amounts"]}
    assert by_page[1]["category"] == "versicherungssumme"
    assert by_page[1]["category_source"] == "table_header"
    # Seite 2 steht nicht in der Tabelle → Fließtext-Kategorie bleibt
    assert by_page[2]["category"] == "sublimit"
    assert by_page[2]["category_source"] == "proximity"


# ── Robustheit gegen eigene Anker und OCR-Trennungen ─────────────────────


def test_bbox_anchors_do_not_hide_signal_words() -> None:
    """Unsere eigenen BBox-Anker sind ~40 Zeichen lang und dürfen das
    Kontextfenster nicht auffressen."""
    text = (
        '<a id="odl-p9-bbox-96.5-394.9-242.2-405.8"></a> '
        "Selbstbeteiligung: 1.000 EUR je Schadenfall"
    )
    e = extract_entities(_result_from_text(text))
    assert e["amounts"][0]["category"] == "selbstbehalt"


def test_signal_word_split_by_ocr_still_matches() -> None:
    """PDF-Extraktion fügt bei weiter Laufweite Leerzeichen in Wörter ein."""
    e = extract_entities(_result_from_text("Sub limit: 20.000.000 EUR je Fall"))
    assert e["amounts"][0]["category"] == "sublimit"


def test_versicherungssumme_split_by_ocr() -> None:
    e = extract_entities(_result_from_text("Versicherungssum me: 5.000.000 EUR"))
    assert e["amounts"][0]["category"] == "versicherungssumme"


def test_short_needles_not_squeezed() -> None:
    """Kurze Signalwörter wie ' sb ' dürfen nicht in fremden Wörtern
    matchen — sonst würde 'Absberg' zu selbstbehalt."""
    e = extract_entities(_result_from_text("In Absberg wurden 750 EUR verbucht."))
    assert e["amounts"][0]["category"] == "unbekannt"


def test_eigenbehalt_recognised() -> None:
    e = extract_entities(
        _result_from_text("Ein genereller unversicherter Eigenbehalt von 1.000 EUR")
    )
    assert e["amounts"][0]["category"] == "selbstbehalt"


def test_entschaedigungsleistung_is_sublimit() -> None:
    e = extract_entities(
        _result_from_text("Die Entschädigungsleistung ist auf max. 10.000 EUR begrenzt")
    )
    assert e["amounts"][0]["category"] == "sublimit"
