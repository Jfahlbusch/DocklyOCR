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

| Schreibweise | Ergebnis |
|---|---|
| `01.05.2026` | `2026-05-01` |
| `1.5.2026` | `2026-05-01` (vierstelliges Jahr genügt) |
| `01.05.27` | `2027-05-01` (Kurzform mit führenden Nullen) |
| `1.5.27` | — **kein** Datum |
| `Ziffer 1.5.24` | — **kein** Datum |

Validierung: Tag 1–31, Monat 1–12. Ungültiges wie `99.99.2026` wird
verworfen.

**Warum die Kurzform führende Nullen braucht:** Versicherungsbedingungen
sind voller Gliederungsnummern wie „Ziffer 1.5.24" oder „Abschnitt
1.12.10". Ohne diese Regel würden sie als Datumsangaben gelesen — bei
einem realen 127-seitigen Versicherungsschein waren das über 100
Fehltreffer. Zusätzlich werden Zahlen verworfen, denen `Ziffer`, `Nr.`,
`Nummer`, `Abschnitt` oder `Punkt` unmittelbar vorausgeht. Ein falsches
Datum ist schlimmer als ein fehlendes.

Ausgeschriebene Monatsnamen (`1. Mai 2026`) werden **nicht** erkannt
(siehe §4 Grenzen).

### 1.4 Kategorien — was ein Wert *bedeutet*

Beträge und Daten tragen zusätzlich ein `category`-Feld. Die Zuordnung
erfolgt über Signalwörter — **nie** über Raten. Findet sich kein
Signalwort, steht dort `unbekannt`.

**Beträge** (`category`):

| Kategorie | Signalwörter (Auswahl) |
|---|---|
| `selbstbehalt` | Selbstbeteiligung, Selbstbehalt, Eigenanteil, SB, Abzugsfranchise |
| `praemie` | Beitrag, Prämie, Jahresbeitrag, Netto-/Bruttobeitrag, Versicherungsteuer |
| `sublimit` | Sublimit, Höchstersatzleistung, Entschädigungsgrenze, begrenzt auf, Erstrisikosumme, max. je |
| `versicherungssumme` | Versicherungssumme, Deckungssumme, Versicherungswert, Haftungssumme, Pauschalsumme |
| `bemessungsgrundlage` | Umsatz, Lohnsumme, Bausumme, Mietwert, Bemessungsgrundlage |
| `unbekannt` | kein Signalwort gefunden |

**Daten** (`category`): `vertragsbeginn`, `vertragsablauf`, `stichtag`,
`unbekannt`.

#### Wie die Zuordnung genau funktioniert

Jeder Wert trägt neben `category` auch ein `category_source`, das zeigt,
**woher** die Kategorie stammt — damit nachgelagerte Systeme entscheiden
können, wie sehr sie ihr trauen:

| `category_source` | Bedeutung | Verlässlichkeit |
|---|---|---|
| `table_header` | aus der JSON-Tabellenstruktur (echtes Spaltenraster inkl. verbundener Zellen) | am höchsten |
| `table_header_md` | aus einer Markdown-Pipe-Tabelle (Fallback ohne `structure.json`, z. B. vLLM-Jobs) | hoch |
| `proximity` | nächstgelegenes Signalwort im Fließtext | mittel |
| `none` | kein Signalwort gefunden → `category` ist `unbekannt` | — |

1. **In Tabellen entscheidet der Spaltenheader.**
   Liegt eine `structure.json` vor (opendataloader-Jobs), wird das echte
   Spaltenraster ausgewertet: Zeile 1 ist die Kopfzeile, `column span`
   wird berücksichtigt (ein über zwei Spalten laufender Header gilt für
   beide). Ohne Strukturdatei greift ersatzweise das Parsen von
   Markdown-Pipe-Tabellen. In

   ```
   |Position|Versicherungssumme|Selbstbehalt|Anteil|
   |---|---|---|---|
   |Feuer|1.500.000,00 EUR|500 EUR|20 %|
   ```

   wird `1.500.000,00 EUR` zu `versicherungssumme` und `500 EUR` zu
   `selbstbehalt` — unabhängig davon, was in den Nachbarzellen steht.
   Sagt der Header nichts Verwertbares (z. B. „Wert A"), bleibt der Wert
   `unbekannt`; die Nachbarzellen würden nur in die Irre führen.

   Die Zuordnung ist **seitenweise** isoliert. Derselbe Betrag steht oft
   zweimal im Dokument — einmal als Versicherungssumme in der Tarif­tabelle
   und Seiten später erneut als Sublimit im Fließtext. Ohne Seitenprüfung
   würde der Tabellenheader das Fließtext-Vorkommen überschreiben.
   Passt ein Wert auf mehrere Zellen derselben Seite, bleibt seine
   Kategorie unverändert (Mehrdeutigkeit).

2. **Im Fließtext gewinnt das nächstgelegene Signalwort.**
   Zuerst wird der Text **vor** dem Wert geprüft (Deutsch stellt das Label
   voran: „Selbstbeteiligung: 500 EUR"), erst danach der Text dahinter.
   Innerhalb eines Fensters zählt die *Nähe*, nicht die Reihenfolge im
   Katalog. Das ist nötig, weil ein 60-Zeichen-Fenster oft mehrere Werte
   umfasst: In „Vertragsbeginn: 01.07.2026, Ablauf: 01.07.2027" stehen vor
   dem zweiten Datum beide Labels — nur die Nähe zeigt, dass es zu
   „Ablauf" gehört.

### 1.5 Bedingungswerke und Rechtsnormen (`references`)

Zwei geschlossene Vokabulare, exakt gematcht:

**Bedingungswerke** — die Standard-Klauselwerke, mit optionalem Jahrgang:
`AFB`, `AERB`, `AWB`, `ASTB`, `MFBU`, `ABE`, `ABMG`, `ABN`, `ABU`, `AHB`,
`BHV`, `AVB-PV`, `AVB-Cyber`, `AVB-WG`, `KFV`, `ULLA`, `D&O`, `VHB`,
`VGB`, `AVBR`, `BBR`, `AMB`, `AStB`, `ARB`, `AKB`.

```json
{"raw": "AFB 2008", "type": "bedingungswerk", "code": "AFB", "year": 2008,
 "page": 12, "context": "Es gelten die AFB 2008 …"}
```

Gematcht wird nur als eigenständiges Wort — `AFBX` oder `KAHB` erzeugen
keinen Treffer.

**Rechtsnormen** — `§ 19 VVG`, `§ 823 Abs. 1 BGB`, `Art. 6 DSGVO`:

```json
{"raw": "§ 19 VVG", "type": "rechtsnorm", "gesetz": "VVG",
 "paragraph": "19", "page": 7, "context": "Anzeigepflicht nach § 19 VVG …"}
```

Erkannte Gesetze: VVG, BGB, HGB, AktG, GmbHG, SGB, ZPO, StGB, WEG, VAG,
AWG, AWV, DSGVO, ProdHaftG, UStG, EStG, InsO, GewO, BImSchG, WHG,
ArbSchG, StVG.

Beides liefert direkt die Vorarbeit für das Klausel-zu-Phase-Mapping und
die Rechtszitat-Whitelist der Gutachter-Pipeline.

### 1.6 Vertrags-/Policennummern (`policy_numbers`)

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
| `category` | bei `amounts`/`dates`: was der Wert bedeutet (s. §1.4), `unbekannt` wenn kein Signalwort |
| `category_source` | woher die Kategorie stammt: `table_header`, `table_header_md`, `proximity` oder `none` |
| `code` / `year` | nur bei `references` vom Typ `bedingungswerk` |
| `gesetz` / `paragraph` | nur bei `references` vom Typ `rechtsnorm` |
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
      "category": "versicherungssumme",
      "category_source": "table_header",
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
  "references": [
    {"raw": "AFB 2008", "type": "bedingungswerk", "code": "AFB", "year": 2008, "page": 12, "context": "..."},
    {"raw": "§ 19 VVG", "type": "rechtsnorm", "gesetz": "VVG", "paragraph": "19", "page": 7, "context": "..."}
  ],
  "meta": {
    "counts": {"amounts": 1, "percentages": 1, "dates": 1, "policy_numbers": 1, "references": 2},
    "amount_categories": {"versicherungssumme": 1},
    "date_categories": {"vertragsbeginn": 1},
    "bedingungswerke": ["AFB"],
    "rechtsnormen": ["§ 19 VVG"],
    "engine": "opendataloader",
    "extractor_version": 2
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
