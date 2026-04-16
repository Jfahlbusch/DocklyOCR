# Pipeline v5 — Batch-Extract, Tabellenerkennung, Kontext-Merge, Inkrementeller Output

**Datum:** 2026-04-16
**Status:** Approved for implementation
**Scope:** In-place refactor von `run_ocr()` in `ocr_pipeline.py`. Kein API-Breaking-Change.

---

## 1. Problem

Große PDFs (50-100+ Seiten) laufen unendlich lange, weil:
- `pdftoppm` pro Seite × pro Strategie aufgerufen wird (Worst Case: Seiten × 13)
- Tabellen als Fließtext erkannt werden — ohne Struktur
- Satzbrüche an Seitengrenzen im Output stehen bleiben
- Die Ausgabedatei erst am Ende komplett geschrieben wird — bei Worker-Crash kein Teilergebnis

## 2. Lösung — Vier Änderungen am Pipeline-Flow

### 2.1 Batch-Extraktion

**Vorher:** `pdftoppm` wird innerhalb der Strategie-Schleife für jede Seite einzeln aufgerufen, bei jedem Strategie-Versuch erneut mit anderer DPI. Worst Case bei 100 Seiten: bis zu 1300 Aufrufe.

**Nachher:** Ein einziger `pdftoppm`-Aufruf extrahiert **alle** Seiten auf einmal bei 150dpi (höchste DPI aus STRATEGIES). Strategien mit niedrigerer DPI (100, 72) erhalten per Pillow-Downscale ein proportional verkleinertes Bild.

```python
def _batch_extract_pages(pdf_path: Path, tmp_dir: Path, dpi: int = 150) -> list[Path]:
    """Extract ALL pages at once. Returns sorted list of page image paths."""
    prefix = tmp_dir / "page"
    subprocess.run(
        ["pdftoppm", "-r", str(dpi), "-jpeg", str(pdf_path), str(prefix)],
        capture_output=True, timeout=120,
    )
    return sorted(tmp_dir.glob("page-*.jpg"))

def _downscale_for_strategy(src_150dpi: Path, target_dpi: int, tmp_dir: Path) -> Path:
    """Proportional downscale: 100dpi → 66%, 72dpi → 48%."""
    scale = target_dpi / 150
    img = Image.open(src_150dpi)
    new_size = (int(img.width * scale), int(img.height * scale))
    img = img.resize(new_size, Image.LANCZOS)
    out = tmp_dir / f"scaled_{target_dpi}dpi_{src_150dpi.name}"
    img.save(out, "JPEG", quality=90)
    return out
```

**Seitenzahl** ergibt sich aus `len(pages)` — kein separater `pdfinfo`-Aufruf.

### 2.2 Tabellenerkennung (Zwei-Pass)

**Schritt 1 — Pattern-Detection** nach erfolgreichem Normal-OCR:

```python
TABLE_INDICATORS = [
    r'\|.*\|.*\|',           # mindestens 3 Pipe-Spalten
    r'\d+[.,]\d{2}\s+\d+',  # Zahlenkolonnen
    r'[-–]{3,}\s*\+',       # horizontale Linien mit Kreuzungen
]

def _detect_table_patterns(text: str) -> bool:
    lines = text.strip().splitlines()
    if len(lines) < 3:
        return False
    matches = sum(
        1 for line in lines
        if any(re.search(pat, line) for pat in TABLE_INDICATORS)
    )
    return matches / len(lines) >= 0.3
```

**Schritt 2 — Spezialisierter Table-OCR** bei Treffer:

```python
def _ocr_table(img_path: Path) -> str:
    """Re-OCR with table-specific prompt. Returns Markdown table."""
    # ... (gleicher httpx-Client wie _call_ollama, aber mit speziellem Prompt)
    prompt = (
        "Extract all tables from this image as Markdown tables using "
        "| delimiters. Keep headers. Output ONLY the table, no explanation."
    )
    # POST an Ollama wie _call_ollama, mit prompt statt "OCR"
```

**Fallback:** Wenn `_ocr_table()` leeren Text liefert oder fehlschlägt → Original-Text bleibt (Heuristik-Fehler schadet nicht).

**PageResult-Erweiterung:**

```python
@dataclass
class PageResult:
    number: int
    text: str | None
    strategy: str
    elapsed_s: float
    is_table: bool = False    # NEU — Default False für Rückwärtskompatibilität
```

`OcrResult.from_json_dict` deserialisiert `is_table` mit Default False wenn das Feld fehlt (alte JSON-Formate).

### 2.3 Kontext-Merge über Seitengrenzen

Nach dem OCR jeder 3er-Gruppe prüft eine Merge-Funktion die Seitengrenzen:

```python
def _merge_across_boundaries(pages: list[PageResult]) -> list[PageResult]:
    for i in range(len(pages) - 1):
        curr, nxt = pages[i], pages[i + 1]
        if curr.text is None or nxt.text is None:
            continue
        if curr.is_table or nxt.is_table:
            continue  # Tabellenseiten nie mergen
        
        last_line = curr.text.rstrip().rsplit("\n", 1)[-1].rstrip()
        first_line = nxt.text.lstrip().split("\n", 1)[0].lstrip()
        
        if not _ends_sentence(last_line) and not _starts_new_section(first_line):
            # Letzte Zeile von curr + erste Zeile von nxt zusammenfügen
            # curr.text wird um letzte Zeile gekürzt
            # nxt.text bekommt die zusammengefügte Zeile vorangestellt
            ...
    return pages

def _ends_sentence(line: str) -> bool:
    return bool(line) and line[-1] in ".!?:;»\""

def _starts_new_section(line: str) -> bool:
    return bool(re.match(r'^(§\s*\d|[A-Z]{2,}|\d+\.\s|[-–•])\s', line))
```

**Chunking mit Overlap:**

```
Seiten: 1  2  3  4  5  6  7  8
Chunk 1: [1, 2, 3]   → merge 1↔2, 2↔3. Schreibe Seiten 1, 2, 3.
Chunk 2: [3, 4, 5]   → merge 3↔4, 4↔5. Schreibe Seiten 4, 5 (Seite 3 bereits geschrieben).
Chunk 3: [5, 6, 7]   → merge 5↔6, 6↔7. Schreibe Seiten 6, 7.
Chunk 4: [7, 8]       → merge 7↔8. Schreibe Seite 8.
```

Overlap von 1 Seite pro Chunk sichert, dass keine Grenze übersprungen wird. Der Merge modifiziert nur `text`-Felder in bereits erzeugten `PageResult`-Objekten. Konservativ: bei Zweifel wird NICHT gemergt.

### 2.4 Inkrementelle Ausgabe

```python
class IncrementalWriter:
    def __init__(self, output_path: Path, fmt: str):
        self.output_path = output_path
        self.fmt = fmt
        self.output_path.write_bytes(b"")

    def append_chunk(self, pages: list[PageResult]) -> None:
        if self.fmt not in ("md", "txt"):
            return  # JSON/TOON erst am Ende
        with open(self.output_path, "a", encoding="utf-8") as f:
            for page in pages:
                if page.text is None:
                    if self.fmt == "md":
                        f.write(f"## Seite {page.number}\n\n")
                        f.write(f"[OCR-Fehler auf Seite {page.number}]\n\n")
                    elif self.fmt == "txt":
                        f.write(f"[OCR-Fehler Seite {page.number}]\f")
                    continue
                if self.fmt == "md":
                    f.write(f"## Seite {page.number}\n\n")
                    f.write(page.text + "\n\n")
                    if not page.is_table:
                        f.write(f"> OCR-Strategie: `{page.strategy}`\n\n")
                elif self.fmt == "txt":
                    f.write(page.text + "\f")

    def finalize(self, result: OcrResult) -> bytes:
        if self.fmt in ("md", "txt"):
            return self.output_path.read_bytes()
        from app.services.formatters import format_output
        body, _ = format_output(result, self.fmt)
        self.output_path.write_bytes(body)
        return body
```

| Format | Inkrementell | Grund |
|---|---|---|
| md | Ja | Reine Aneinanderreihung von `## Seite N` Blöcken |
| txt | Ja | Aneinanderreihung mit `\f` Separator |
| toon | Nein | Braucht `document:` Header mit Gesamtseitenzahl |
| json | Nein | JSON-Struktur braucht vollständigen `meta`-Block |

**Crash-Recovery:** Chunks, die vor einem Crash geschrieben wurden, bleiben als Partial-Result im Storage. Wird nicht als Feature exponiert, hilft aber beim Debugging.

---

## 3. Neuer `run_ocr()`-Flow

```python
def run_ocr(input_path: Path, tmp_dir: Path,
            output_path: Path | None = None,
            output_format: str = "md") -> OcrResult:

    # 1. Batch-Extract (ein pdftoppm-Aufruf für alle Seiten)
    if _is_pdf(input_path):
        page_images = _batch_extract_pages(input_path, tmp_dir)
    else:
        page_images = [input_path]  # Bild-Input: eine "Seite"

    # 2. Writer vorbereiten (optional)
    writer = IncrementalWriter(output_path, output_format) if output_path else None

    # 3. Serieller OCR + Tabellenerkennung pro Seite
    all_pages: list[PageResult] = []
    for i, img_path in enumerate(page_images):
        page_num = i + 1
        result = _ocr_image_with_strategies(img_path, tmp_dir, page_num)

        # Tabellen-Zwei-Pass
        if result.text and _detect_table_patterns(result.text):
            table_text = _ocr_table(img_path)
            if table_text.strip():
                result.text = table_text
                result.is_table = True

        all_pages.append(result)

        # 4. Alle 3 Seiten: Merge + inkrementelles Schreiben
        if len(all_pages) >= 3 and len(all_pages) % 3 == 0:
            chunk = all_pages[-3:]
            _merge_across_boundaries(chunk)
            if writer:
                write_start = 0 if len(all_pages) == 3 else 1
                writer.append_chunk(chunk[write_start:])

    # 5. Restliche Seiten (letzter unvollständiger Chunk)
    remainder = len(all_pages) % 3
    if remainder > 0:
        chunk = all_pages[-remainder:]
        if len(all_pages) > remainder:
            chunk = [all_pages[-(remainder + 1)]] + chunk
            _merge_across_boundaries(chunk)
            if writer:
                writer.append_chunk(chunk[1:])
        else:
            _merge_across_boundaries(chunk)
            if writer:
                writer.append_chunk(chunk)

    # 6. OcrResult
    pages_ok = sum(1 for p in all_pages if p.text)
    ocr_result = OcrResult(
        pages=all_pages,
        page_count=len(all_pages),
        pages_ok=pages_ok,
        pages_failed=len(all_pages) - pages_ok,
    )

    # 7. Finalize (JSON/TOON komplett schreiben)
    if writer:
        writer.finalize(ocr_result)

    return ocr_result
```

**Signatur-Änderung:** Zwei optionale Parameter (`output_path`, `output_format`). Ohne diese → Verhalten identisch zu v4. Kein Breaking Change.

---

## 4. Betroffene Dateien

| Datei | Art der Änderung |
|---|---|
| `app/services/ocr_pipeline.py` | Refactor: Neue Funktionen (`_batch_extract_pages`, `_downscale_for_strategy`, `_detect_table_patterns`, `_ocr_table`, `_merge_across_boundaries`, `_ends_sentence`, `_starts_new_section`, `IncrementalWriter`), refactored `run_ocr()`, `PageResult.is_table` Feld |
| `app/services/formatters.py` | Klein: `is_table`-Check bei MD-Format (keine Strategie-Annotation), `from_json_dict` toleriert fehlendes Feld |
| `app/services/ocr_runner.py` | Klein: neue CLI-Argumente `--output-path`, `--output-format` durchreichen |
| `app/workers/ocr_worker.py` | Klein: `output_path` und `output_format` an Subprozess-Aufruf übergeben |
| `tests/test_pipeline.py` | Erweitern: Tests für Batch-Extract, Table-Detection, Merge, IncrementalWriter |
| `tests/test_formatters.py` | Erweitern: Test für `is_table=True` Seiten |

## 5. NICHT betroffen

- `app/routers/ocr.py`, `app/routers/jobs.py`, `app/routers/admin.py` — keine Änderung
- `app/models.py`, `app/db.py`, `app/auth.py` — keine Änderung
- `app/schemas.py` — keine Änderung
- Admin-UI Templates — keine Änderung
- `docker-compose.yml`, `Dockerfile` — keine Änderung
- API-Contract (Request/Response-Shapes) — keine Änderung

## 6. Performance-Erwartung

| Dokument | v4 (aktuell) | v5 (neu) | Hauptgrund |
|---|---|---|---|
| 2 Seiten, keine Tabellen | ~10s | ~8s | 1× pdftoppm statt 2× |
| 20 Seiten, 2 Tabellen | ~2-3 min | ~1.5-2 min | Batch-Extract + 2 Table-Calls |
| 100 Seiten, 10 Tabellen | ~15-20 min | ~8-12 min | 1× pdftoppm statt ~200×, +10 Table-Calls |

## 7. Rückwärtskompatibilität

- `run_ocr(input_path, tmp_dir)` ohne neue Params → identisches Verhalten wie v4
- `OcrResult.to_json_dict()` → enthält `is_table` Feld (neues Feld, additive Änderung)
- `OcrResult.from_json_dict()` → `is_table` Default False wenn Feld fehlt
- Alle bestehenden 117 Tests müssen weiterhin grün sein

## 8. Bekannte Limitierungen

1. **Tabellenerkennung ist heuristisch** — False Positives durch Fallback aufgefangen (Original-Text bleibt wenn Table-OCR leer)
2. **Merge-Heuristik ist konservativ** — bei Zweifel wird NICHT gemergt
3. **JSON/TOON nicht inkrementell** — brauchen Gesamtstruktur
4. **Kein OCR-Parallelismus** — Ollama serialisiert bei Single-GPU
5. **Table-Prompt ist generisch** — für VVG-Dokumente gut, für exotische Tabellenformate ggf. Prompt-Tuning nötig
