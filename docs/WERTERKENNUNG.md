# Werterkennung — wie DocklyOCR Zahlen und Werte erkennt

Jeder abgeschlossene OCR-Job erzeugt zusätzlich zum Textergebnis ein
Sidecar **`entities.json`**, das alle erkannten Werte maschinenlesbar
aufbereitet. Abrufbar über:

```
GET /v1/jobs/{job_id}/entities        (Header: X-API-Key)
```

Die URL steht auch im Job-Response als `entities_url`, sobald der Job
`done` ist.

**Implementierung:** `app/services/entity_extractor.py` — rein
Regex-basiert und deterministisch. Es wird nur erkannt, was wortwörtlich
im extrahierten Text steht. Kein LLM, keine Interpretation, keine
Halluzination möglich.

---

## 1. Erkannte Werttypen

### 1.1 Geldbeträge (`amounts`)

Ein Betrag wird **nur** erkannt, wenn eine Währungsangabe direkt dabei
steht — nackte Zahlen (Seitenzahlen, Ziffern in Klauseln) werden bewusst
ignoriert.

| Schreibweise im Dokument | erkannter Wert (`value`) | Bemerkung |
|---|---|---|
| `1.500.000,00 EUR` | `1500000.0` | deutsches Vollformat |
| `500 €` | `500.0` | Euro-Zeichen nachgestellt |
| `EUR 12.500,50` | `12500.5` | Währung vorangestellt |
| `2.345,-- EUR` | `2345.0` | `,--` und `,-` gelten als `,00` (Versicherungs-Notation) |
| `1,5 Mio. EUR` | `1500000.0` | Millionen-Kurzform (auch `Mrd`) |
| `500 T€` / `500 TEUR` | `500000.0` | Tausender-Kurzform |
| `Auf Seite 1500 stehen 42 Positionen` | — | **kein** Treffer: keine Währung |

Normalisierungsregeln:
- Punkt = Tausendertrenner, Komma = Dezimaltrenner (deutsches Format)
- `value` ist immer ein kanonischer Float, `currency` immer `"EUR"`
- Damit ist die klassische Faktor-1000-Verwechslung (de `1.500` = 1500
  vs. en `1.500` = 1,5) für nachgelagerte Systeme eliminiert

### 1.2 Prozentwerte (`percentages`)

| Schreibweise | Wert |
|---|---|
| `20 %` | `20.0` |
| `12,5%` | `12.5` |

Leerzeichen vor dem `%` ist optional; Dezimalstellen mit Komma.

### 1.3 Datumsangaben (`dates`)

| Schreibweise | normalisiert (`iso`) |
|---|---|
| `01.05.2026` | `2026-05-01` |
| `1.5.27` | `2027-05-01` (zweistellige Jahre → `20xx`) |

Validierung: Tag 1–31, Monat 1–12. Ungültiges wie `99.99.2026` wird
verworfen. Ausgeschriebene Monatsnamen (`1. Mai 2026`) werden aktuell
**nicht** erkannt (siehe §4 Grenzen).

### 1.4 Vertrags-/Policennummern (`policy_numbers`)

Nur **label-verankert** — eine Nummer wird ausschließlich erkannt, wenn
direkt davor eine erkennbare Beschriftung steht. Das verhindert, dass
beliebige Ziffernfolgen als Vertragsnummern klassifiziert werden.

Erkannte Labels (Groß-/Kleinschreibung egal):
`Versicherungsschein-Nr`, `Vertragsnummer`, `Vertrags-Nr`,
`Policennummer`, `Police-Nr`, `Schein-Nr`, `Antragsnummer` (und
Varianten mit `Nummer`/`Nr.`/`No.`).

| Text im Dokument | erkannt |
|---|---|
| `Versicherungsschein-Nr.: AB-123456/78` | `AB-123456/78` |
| `Vertragsnummer: 4711.0815` | `4711.0815` |
| `Es gelten die Ziffern 123456 der AVB` | — (kein Label) |

Die Nummer muss mindestens eine Ziffer enthalten und 4–31 Zeichen lang
sein; erlaubt sind Großbuchstaben, Ziffern, `-`, `.`, `/`.

---

## 2. Felder pro Wert

Jeder Eintrag trägt:

| Feld | Bedeutung |
|---|---|
| `raw` | die exakte Zeichenkette aus dem Dokument |
| `value` / `iso` | normalisierter Wert (Float bzw. ISO-8601-Datum) |
| `currency` | bei Beträgen immer `"EUR"` |
| `page` | Seite im OCR-Ergebnis (1-basiert) |
| `context` | ±60 Zeichen Umgebungstext — zeigt, *wozu* der Wert gehört (z. B. „Versicherungssumme Feuer: …") |
| `bbox` + `pdf_page` | **nur opendataloader-Jobs:** exakte Koordinaten [x1, y1, x2, y2] im Original-PDF, wenn der Wert eindeutig einem Element zuordenbar war |
| `label` | nur bei `policy_numbers`: die gefundene Beschriftung |

Dazu ein `meta`-Block mit Zählern pro Typ, der Engine, die den Text
erzeugt hat, und der Extractor-Version.

### Beispiel

```json
{
  "amounts": [
    {
      "raw": "1.500.000,00 EUR",
      "value": 1500000.0,
      "currency": "EUR",
      "page": 3,
      "context": "Versicherungssumme Feuer: 1.500.000,00 EUR je Schadenfall",
      "bbox": [88.1, 553.0, 295.8, 568.5],
      "pdf_page": 3
    }
  ],
  "percentages": [
    {"raw": "20 %", "value": 20.0, "page": 5, "context": "Mitversicherung 20 % Anteil"}
  ],
  "dates": [
    {"raw": "01.05.2026", "iso": "2026-05-01", "page": 1, "context": "Vertragsbeginn: 01.05.2026"}
  ],
  "policy_numbers": [
    {"raw": "AB-123456/78", "label": "Versicherungsschein-Nr.", "page": 1, "context": "..."}
  ],
  "meta": {
    "counts": {"amounts": 1, "percentages": 1, "dates": 1, "policy_numbers": 1},
    "engine": "opendataloader",
    "extractor_version": 1
  }
}
```

---

## 3. Dubletten-Behandlung

Derselbe Wert (`raw`) auf derselben Seite wird nur **einmal** gelistet
(z. B. wenn ein Betrag in Tabelle und Fußnote steht). Derselbe Wert auf
**verschiedenen** Seiten bleibt mehrfach erhalten — die Seitenzuordnung
ist Teil der Information.

---

## 4. Grenzen — was NICHT erkannt wird

1. **Falsch gelesene Zahlen bei Scans:** Bei `engine=vllm` (Scans,
   Fotos) stammt der Text aus einem Vision-LLM. Liest das Modell
   `1.500` statt `7.500`, normalisieren wir eine falsche Zahl. Das
   `meta.engine`-Feld zeigt an, wie vertrauenswürdig die Quelle ist:
   `opendataloader` = byte-genau aus dem PDF-Text-Layer,
   `vllm` = OCR-Vertrauensniveau.
2. **Ausgeschriebene Monatsnamen** (`1. Mai 2026`) und relative Angaben
   („zum Monatsersten") werden nicht erkannt.
3. **Fremdwährungen** (USD, CHF, GBP) werden aktuell nicht erfasst —
   nur EUR-Notationen.
4. **Beträge ohne Währungsmarker** („Selbstbeteiligung: 500") werden
   bewusst nicht erfasst (False-Positive-Vermeidung).
5. **Zusammengesetzte Angaben** („max. 2 × 5 Mio. EUR p. a.") — erkannt
   wird `5 Mio. EUR`, die Maximierung `2 ×` steht nur im `context`.
6. **BBox-Zuordnung ist best-effort:** kommt derselbe `raw`-String in
   mehreren PDF-Elementen vor, wird keine BBox gesetzt (Mehrdeutigkeit).

---

## 5. Erweiterung

Neue Werttypen (z. B. Quadratmeter, Mitarbeiterzahlen, Jahresumsätze)
werden als zusätzliche Regex + Extraktionsfunktion in
`app/services/entity_extractor.py` ergänzt und tauchen als neuer
Schlüssel im JSON auf. Die `extractor_version` in `meta` wird dabei
hochgezählt, damit Konsumenten Format-Änderungen erkennen können.
