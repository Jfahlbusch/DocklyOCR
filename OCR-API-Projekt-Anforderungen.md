# OCR-API-Projekt — Anforderungen & Umsetzungs-Prompts

**Projekt:** Self-hosted OCR-API auf Basis von `glm-ocr` (Ollama) mit Multi-Strategy Fallback-Pipeline, API-Key-Authentifizierung, Webhook-Delivery und minimaler Admin-Oberfläche.

**Ziel:** Eine produktionsreife, per OpenAPI/Swagger dokumentierte REST-API, die PDFs und Bilder entgegennimmt, per `glm-ocr` verarbeitet und die Ausgabe in einem wählbaren Format (MD, TXT, TOON, JSON) zurückliefert — synchron oder asynchron per Webhook.

**Umsetzung:** Vollständig mit Claude Code, in mehreren klar abgegrenzten Phasen.

---

## 1. Business-Anforderungen

### 1.1 MVP-Scope (jetzt)
- API akzeptiert PDF und Bilddateien (JPG, PNG, TIFF) bis 100 MB
- OCR-Verarbeitung basiert auf der bestehenden Pipeline v4 (13-Strategien-Fallback mit glm-ocr)
- Ausgabeformate: **MD**, **TXT**, **TOON**, **JSON**
- Synchrones Warten auf Ergebnis ODER asynchrone Verarbeitung mit Webhook-Callback
- API-Key-Authentifizierung pro Kunde (Header: `X-API-Key`)
- Minimale Admin-Web-UI zum Anlegen/Löschen von Kunden und API-Keys
- Swagger/OpenAPI-Dokumentation unter `/docs`
- Self-hostable per `docker compose up`

### 1.2 Perspektivisch (später, Architektur muss es ermöglichen)
- Paid-API mit Stripe-Anbindung
- Usage-Tracking (Pages pro Monat, pro API-Key)
- Rate-Limiting pro API-Key
- Tiered Plans (Free / Pro / Enterprise)
- Statistik-Dashboard pro Kunde

### 1.3 Out-of-Scope
- Kein öffentliches Self-Service-Signup im MVP — Kunden werden vom Admin angelegt
- Keine Bezahlung im MVP
- Keine Mandantentrennung auf Infrastruktur-Ebene (alle Kunden auf einer DB)

---

## 2. Technischer Stack

| Komponente | Technologie | Begründung |
|---|---|---|
| API-Framework | **FastAPI** (Python 3.11+) | Built-in OpenAPI/Swagger, async, schnelle File-Uploads, gut mit Pydantic |
| OCR-Engine | **Ollama + glm-ocr** | Bestehend, bewährt, lokal |
| PDF → Image | **pdftoppm** (poppler-utils) | Bestehend in Pipeline v4 |
| Image-Processing | **Pillow** | Graustufen, Resize, Split |
| DB | **SQLite** (MVP) → später Postgres | Einfach zu deployen, leicht migrierbar |
| ORM | **SQLModel** | FastAPI-nativ, Pydantic + SQLAlchemy |
| Job-Queue (async) | **ARQ** (Redis-basiert) | Einfach, async-native, kein Celery-Overhead |
| Admin-UI | **Jinja2 + HTMX + Tailwind (CDN)** | Minimal, kein Build-Step, schnell umgesetzt |
| Auth (Admin-UI) | **Session-Cookie + Passwort-Hash** | Keine externen Services |
| Container | **Docker + docker-compose** | Standardisiertes Deployment |
| Reverse Proxy | **Caddy** (empfohlen) oder Nginx | Automatisches HTTPS |

**Projektstruktur:**
```
ocr-api/
├── app/
│   ├── main.py                  # FastAPI app
│   ├── config.py                # Settings (pydantic-settings, .env)
│   ├── db.py                    # SQLModel engine + session
│   ├── models.py                # Customer, ApiKey, Job, JobStatus
│   ├── auth.py                  # API-Key + Admin-Session auth
│   ├── routers/
│   │   ├── ocr.py               # /v1/ocr endpoints
│   │   ├── jobs.py              # /v1/jobs/{id} endpoints
│   │   └── admin.py             # /admin/* (HTML pages)
│   ├── services/
│   │   ├── ocr_pipeline.py      # Pipeline v4 gekapselt
│   │   ├── formatters.py        # MD, TXT, TOON, JSON Writer
│   │   ├── webhook.py           # Webhook-Delivery mit Retry
│   │   └── storage.py           # File-Storage (lokal, S3-kompatibel vorbereitet)
│   ├── workers/
│   │   └── ocr_worker.py        # ARQ Worker
│   └── templates/               # Jinja2 Admin-UI
├── tests/
├── docker-compose.yml
├── Dockerfile
├── pyproject.toml
└── README.md
```

---

## 3. Datenmodell

### 3.1 Customer
| Feld | Typ | Beschreibung |
|---|---|---|
| id | int, PK | |
| name | str | Firmenname |
| email | str, unique | Kontakt-E-Mail |
| created_at | datetime | |
| is_active | bool | Soft-disable |
| plan | str, default `"free"` | Vorbereitung für Paid |
| monthly_page_limit | int, nullable | Platz für Quota |

### 3.2 ApiKey
| Feld | Typ | Beschreibung |
|---|---|---|
| id | int, PK | |
| customer_id | FK → Customer | |
| key_hash | str | **SHA-256 Hash**, niemals Klartext speichern |
| key_prefix | str(8) | Erste 8 Zeichen zum Anzeigen (`sk_live_abcd...`) |
| name | str | z. B. "Produktion", "Test" |
| created_at | datetime | |
| last_used_at | datetime, nullable | |
| is_active | bool | |

Klartext wird **nur einmal** bei der Erstellung angezeigt.

### 3.3 Job
| Feld | Typ | Beschreibung |
|---|---|---|
| id | str (UUID), PK | |
| api_key_id | FK → ApiKey | |
| customer_id | FK → Customer | Denormalisiert für schnelle Queries |
| status | enum: `pending`, `processing`, `done`, `failed` | |
| input_filename | str | |
| input_size_bytes | int | |
| input_mime | str | |
| output_format | enum: `md`, `txt`, `toon`, `json` | |
| webhook_url | str, nullable | |
| created_at | datetime | |
| started_at | datetime, nullable | |
| finished_at | datetime, nullable | |
| page_count | int, nullable | |
| pages_ok | int, nullable | |
| pages_failed | int, nullable | |
| error_message | str, nullable | |
| result_path | str, nullable | Pfad zur Ausgabedatei im Storage |
| webhook_delivered | bool, default false | |
| webhook_attempts | int, default 0 | |

### 3.4 AdminUser
Ein-User-System im MVP (`admin`), Passwort-Hash per `passlib`/`bcrypt`.

---

## 4. API-Endpunkte

Alle `/v1/*`-Endpunkte erfordern `X-API-Key`-Header.

### 4.1 OCR-Submission

**`POST /v1/ocr`** — Datei einreichen

- Content-Type: `multipart/form-data`
- Felder:
  - `file` (required): PDF oder Bild
  - `output_format` (required): `md` | `txt` | `toon` | `json`
  - `mode` (optional, default `async`): `sync` | `async`
  - `webhook_url` (optional): Nur relevant bei `async`; wenn gesetzt, wird nach Fertigstellung ein POST an diese URL geschickt

**Response (mode=async):** `202 Accepted`
```json
{
  "job_id": "7c9e6f8d-...",
  "status": "pending",
  "status_url": "/v1/jobs/7c9e6f8d-..."
}
```

**Response (mode=sync):** `200 OK` mit Content-Type des gewählten Formats, Body = fertiges Ergebnis. Timeout serverseitig z. B. 300 s.

### 4.2 Job-Status & Ergebnis

**`GET /v1/jobs/{job_id}`** — Status + Metadaten
```json
{
  "job_id": "7c9e6f8d-...",
  "status": "done",
  "created_at": "...",
  "finished_at": "...",
  "output_format": "md",
  "page_count": 43,
  "pages_ok": 43,
  "pages_failed": 0,
  "result_url": "/v1/jobs/7c9e6f8d-.../result"
}
```

**`GET /v1/jobs/{job_id}/result`** — Ergebnisdatei herunterladen
- Content-Type je nach Format
- `Content-Disposition: attachment; filename="<original>.<ext>"`

**`GET /v1/jobs`** — Eigene Jobs auflisten (paginiert)

### 4.3 Webhook-Payload

Beim Abschluss eines async-Jobs sendet der Server **`POST <webhook_url>`** mit:

```json
{
  "job_id": "7c9e6f8d-...",
  "status": "done",
  "output_format": "md",
  "page_count": 43,
  "pages_ok": 43,
  "pages_failed": 0,
  "result_url": "https://api.example.com/v1/jobs/7c9e6f8d-.../result",
  "finished_at": "2026-04-15T12:34:56Z"
}
```

**Header:**
- `X-Signature: sha256=<hmac>` — HMAC-SHA256 mit kundenspezifischem Webhook-Secret (optional im MVP, Hook vorbereiten)
- `User-Agent: ocr-api-webhook/1.0`

**Retry-Strategie:** 3 Versuche mit exponentiellem Backoff (30 s, 2 min, 10 min). `webhook_attempts` zählt hoch. Bei Status ≥ 2xx = Erfolg.

### 4.4 Health

**`GET /health`** — öffentlich, für Reverse-Proxy
```json
{"status": "ok", "ollama": "ok", "db": "ok"}
```

---

## 5. Admin-Oberfläche

**Pfad:** `/admin/*` — eigener Router, Session-Auth.

### 5.1 Seiten (Server-rendered Jinja2)

| Pfad | Inhalt |
|---|---|
| `GET /admin/login` | Login-Formular |
| `POST /admin/login` | Session erstellen |
| `POST /admin/logout` | Session löschen |
| `GET /admin` | Dashboard: Kundenanzahl, Jobs heute/Woche/Monat |
| `GET /admin/customers` | Liste aller Kunden, „+ Neuer Kunde" |
| `POST /admin/customers` | Neuen Kunden anlegen |
| `GET /admin/customers/{id}` | Kundendetail + API-Keys + letzte Jobs |
| `POST /admin/customers/{id}/keys` | Neuen API-Key erzeugen (zeigt Klartext **einmal**) |
| `POST /admin/customers/{id}/keys/{key_id}/revoke` | Key deaktivieren |
| `GET /admin/jobs` | Alle Jobs system-weit, filterbar |

### 5.2 UI-Anforderungen
- Minimaler Look mit **Tailwind via CDN** (kein Build)
- **HTMX** für Formular-Submits ohne Page-Reload
- Keine Farbexplosion: neutral grau, Primär-Button in Indigo
- Mobile-tauglich, aber primär Desktop
- Copy-to-Clipboard-Button für neu erstellte API-Keys

---

## 6. OCR-Pipeline-Kapselung

Die bestehende Pipeline v4 wird als `services/ocr_pipeline.py` gekapselt, mit folgender Schnittstelle:

```python
@dataclass
class OcrResult:
    pages: list[PageResult]          # text, strategy, elapsed
    page_count: int
    pages_ok: int
    pages_failed: int

def run_ocr(input_path: Path, tmp_dir: Path) -> OcrResult:
    ...
```

Die 13 Strategien, Split-Logik und Grayscale-Behandlung bleiben 1:1 erhalten. Pipeline ist vollständig synchron pro Job, wird aber vom ARQ-Worker in einem Subprozess ausgeführt, damit Ollama-Crashes einen Worker nicht komplett abschießen.

**Formatter-Interface:**

```python
def format_output(result: OcrResult, fmt: Literal["md","txt","toon","json"]) -> bytes:
    ...
```

- **md** — bestehende Markdown-Ausgabe mit `## Seite N` Headings
- **txt** — Plain text, Seiten durch Form-Feed (`\f`) getrennt
- **toon** — bestehendes TOON-Format mit `page[N]:` + §-Section-Detection
- **json** — `{"pages": [{"number": 1, "text": "...", "strategy": "150dpi/1024px"}, ...], "meta": {...}}`

---

## 7. Sicherheit & Betrieb

- API-Keys werden **nur als SHA-256-Hash** in der DB gespeichert
- Rate-Limit per Key (MVP: simpel in-memory, später Redis) — z. B. 10 Requests/Minute
- Upload-Limit 100 MB, über `starlette` Middleware erzwungen
- Content-Type-Whitelist: `application/pdf`, `image/jpeg`, `image/png`, `image/tiff`
- Ergebnisdateien werden nach **30 Tagen** automatisch gelöscht (Cron-Task)
- Logging strukturiert als JSON (stdlib + `python-json-logger`)
- Environment-Variablen: `.env` (nie committen), `env.example` liegt im Repo
- Backups: SQLite-Datei wird per Cron gesichert
- CORS: standardmäßig geschlossen, per Env-Var `ALLOWED_ORIGINS` öffenbar

---

## 8. Umsetzungs-Phasen & Claude-Code-Prompts

Jede Phase ist ein eigener Claude-Code-Lauf. Prompts sind so formuliert, dass du sie **1:1 in Claude Code einfügen** kannst, sobald du im Projektverzeichnis bist.

### Phase 0 — Projekt-Setup

**Prompt:**
> Erstelle ein neues Python-Projekt `ocr-api` mit **FastAPI**, **SQLModel**, **ARQ**, **Pillow**, **httpx**, **python-multipart**, **passlib[bcrypt]**, **jinja2**, **pydantic-settings** und **python-json-logger** als Dependencies. Nutze `pyproject.toml` mit `uv` oder `pip-tools`. Lege die Ordnerstruktur gemäß folgender Vorgabe an: `app/`, `app/routers/`, `app/services/`, `app/workers/`, `app/templates/`, `tests/`. Erstelle eine minimale `app/main.py` mit einer FastAPI-Instanz und einem `/health`-Endpunkt, der `{"status":"ok"}` zurückgibt. Füge ein `Dockerfile` (Python 3.11-slim, installiert `poppler-utils`) und eine `docker-compose.yml` mit Services `api`, `worker`, `redis`, sowie einem Named Volume `./data` für SQLite und uploads. Erstelle eine `.env.example` mit allen benötigten Variablen: `DATABASE_URL`, `REDIS_URL`, `OLLAMA_URL`, `OLLAMA_MODEL=glm-ocr`, `ADMIN_USERNAME`, `ADMIN_PASSWORD_HASH`, `SESSION_SECRET`, `STORAGE_DIR`, `ALLOWED_ORIGINS`. Schreibe eine `README.md`, die `docker compose up` als Start-Befehl dokumentiert.

### Phase 1 — DB-Modelle & Migrationen

**Prompt:**
> Implementiere in `app/models.py` die SQLModel-Klassen `Customer`, `ApiKey`, `Job` und `AdminUser` exakt gemäß dem Datenmodell in `OCR-API-Projekt-Anforderungen.md` Abschnitt 3. Nutze für `Job.status` ein `str`-Enum mit Werten `pending|processing|done|failed` und für `Job.output_format` ein Enum mit `md|txt|toon|json`. Implementiere in `app/db.py` die Engine-Erstellung (SQLite, File-basiert aus `DATABASE_URL`), `init_db()` (erzeugt alle Tabellen) und einen `get_session()`-Dependency-Generator. Schreibe ein kurzes CLI-Script `scripts/init_db.py`, das die DB erzeugt und einen Admin-User aus den Env-Variablen anlegt. Schreibe Pytest-Tests, die das Erzeugen eines Customers, eines ApiKey und eines Jobs verifizieren.

### Phase 2 — API-Key-Authentifizierung

**Prompt:**
> Implementiere in `app/auth.py`:
> 1. `generate_api_key()` — erzeugt Klartext-Key `sk_live_` + 32 zufällige URL-safe Zeichen, gibt `(plaintext, hash, prefix)` zurück
> 2. FastAPI-Dependency `require_api_key`, die den `X-API-Key`-Header liest, den Hash berechnet, in der DB sucht, `last_used_at` aktualisiert und das zugehörige `ApiKey`+`Customer`-Objekt in den Request-State legt
> 3. Gib bei ungültigem oder fehlendem Key `401` zurück
> 4. Schreibe Pytest-Tests für beide Fälle

### Phase 3 — OCR-Pipeline-Kapselung

**Prompt:**
> Nutze als Vorlage den vollständigen Pipeline-v4-Code aus **Anhang C** dieses Dokuments und portiere die 13-Strategien-Pipeline in `app/services/ocr_pipeline.py`. Die Funktion `run_ocr(input_path: Path, tmp_dir: Path) -> OcrResult` muss:
> 1. Wenn Input PDF: per `pdftoppm` seitenweise extrahieren und die 13 Strategien gemäß `STRATEGIES`-Liste durchlaufen, bis eine funktioniert
> 2. Wenn Input Bild: dieselben Strategien auf das einzelne Bild anwenden
> 3. Pro Seite einen `PageResult(number, text, strategy, elapsed_s)` zurückgeben
> 4. Am Ende ein `OcrResult(pages, page_count, pages_ok, pages_failed)` zurückgeben
> 
> Halte Split-Logik, Grayscale-Conversion und alle Strategien **exakt** wie in v4. Ollama-URL und Model-Name aus Settings. Schreibe Unit-Tests mit einem Mock für die Ollama-HTTP-Calls.

### Phase 4 — Output-Formatter

**Prompt:**
> Implementiere in `app/services/formatters.py` die Funktion `format_output(result: OcrResult, fmt: str) -> tuple[bytes, str]`, die `(body_bytes, mime_type)` zurückgibt, für die Formate:
> - **md**: Markdown mit `## Seite N` Headings, pro Seite Text + eine Zeile `> OCR-Strategie: {strategy}` in grau
> - **txt**: Plain text, Seiten durch `\f` getrennt
> - **toon**: exakt das TOON-Format aus der v4-Pipeline (inkl. §-Detection)
> - **json**: `{"meta":{page_count,pages_ok,pages_failed},"pages":[{"number","text","strategy","elapsed_s"}]}` (utf-8, indent=2)
> 
> Schreibe Pytest-Tests mit einem fixen `OcrResult`-Fixture und Snapshot-Vergleich pro Format.

### Phase 5 — OCR-Endpunkte & Job-Queue

**Prompt:**
> Implementiere:
> 1. `app/routers/ocr.py` mit `POST /v1/ocr` — nimmt File, `output_format`, `mode`, `webhook_url` entgegen. Legt einen `Job` in der DB an. Bei `mode=async`: Datei ins Storage speichern, Job in ARQ enqueuen, `202` mit `job_id` zurückgeben. Bei `mode=sync`: `ocr_pipeline.run_ocr` direkt aufrufen, Formatter anwenden, fertiges Binary zurückgeben.
> 2. `app/routers/jobs.py` mit `GET /v1/jobs/{id}`, `GET /v1/jobs/{id}/result`, `GET /v1/jobs` (paginiert, nur eigene).
> 3. `app/workers/ocr_worker.py` — ARQ-Task `process_ocr_job(job_id)`: lädt Job, führt Pipeline in Subprozess aus (damit Ollama-Crashes den Worker nicht killen), schreibt Ergebnis ins Storage, updated Job-Status, triggert Webhook wenn `webhook_url` gesetzt.
> 4. `app/services/storage.py` — schlanke Klasse mit `save(file, job_id) -> Path` und `load(job_id, kind) -> Path`, lokal FS, aber Interface so, dass man später S3 anflanschen kann.
> 
> Binde Upload-Size-Limit (100 MB) als Middleware ein. Mime-Type-Whitelist im Endpunkt.

### Phase 6 — Webhook-Delivery

**Prompt:**
> Implementiere `app/services/webhook.py`:
> 1. `deliver_webhook(job_id)` — lädt den Job, baut das Payload gemäß Spec Abschnitt 4.3, berechnet optional `X-Signature: sha256=<hmac>` mit kundenspezifischem Secret (Feld `Customer.webhook_secret`, nullable, im MVP einfach übergeben oder leer lassen), POSTet per `httpx` mit Timeout 15 s.
> 2. Retry-Logik: bei Fehler (Status != 2xx oder Exception) Job-Feld `webhook_attempts` inkrementieren und per ARQ `defer` mit Delay 30 s / 2 min / 10 min erneut versuchen. Nach 3 Fehlversuchen aufgeben, Feld `webhook_delivered=false` lassen, aber Job-Status bleibt `done`.
> 3. Integriere den Aufruf am Ende von `process_ocr_job`.
> 4. Schreibe Tests mit `respx` oder `httpx.MockTransport`.

### Phase 7 — Admin-UI

**Prompt:**
> Implementiere `app/routers/admin.py` mit allen Routen aus Spec Abschnitt 5.1. Nutze Jinja2-Templates unter `app/templates/admin/`, Tailwind per CDN-Link im `base.html`, HTMX per CDN. Session-Auth über `itsdangerous` Signed Cookie. Login-Seite prüft Username + `bcrypt`-Passwort gegen `AdminUser`. Implementiere:
> - Dashboard mit Counter-Cards (Kunden, Jobs heute, Jobs Woche, Jobs Monat)
> - Kundenliste + Formular „Neuer Kunde"
> - Kundendetail mit API-Keys-Tabelle + Button „Neuer Key" → Modal zeigt Klartext **einmalig** mit Copy-Button
> - Key-Revoke per POST
> - Globale Jobliste mit Status-Filter
> 
> Design: minimalistisch, neutral grau, Primär-Indigo, mobile-tauglich. Kein JS-Build nötig.

### Phase 8 — OpenAPI-Polish & README

**Prompt:**
> Gehe alle FastAPI-Routen durch und füge `summary`, `description`, `response_model`, `responses`-Beispiele und `tags` hinzu, sodass `/docs` (Swagger) und `/redoc` aussagekräftig sind. Füge `openapi_tags` mit Beschreibungen für `ocr`, `jobs`, `admin`, `health` in der FastAPI-Instanz hinzu. Aktualisiere die `README.md` mit:
> - Projektüberblick
> - Voraussetzungen (Docker, Ollama mit `glm-ocr` Model)
> - `docker compose up` Quickstart
> - Admin-Zugang & erste Schritte
> - cURL-Beispiele für Sync und Async Upload
> - Webhook-Payload-Dokumentation
> 
> Erstelle einen GitHub-Actions-Workflow `.github/workflows/ci.yml`, der Lint (ruff), Format-Check (ruff format) und Pytest ausführt.

### Phase 9 — Tests & Verifikation

**Prompt:**
> Schreibe Integrationstests in `tests/test_e2e.py` mit `httpx.AsyncClient`:
> 1. Kunden + Key anlegen, mit diesem Key ein kleines Test-PDF (2 Seiten, liegt unter `tests/fixtures/sample.pdf`) per `POST /v1/ocr` im Sync-Modus für alle vier Formate uploaden und Response validieren
> 2. Dieselbe Datei im Async-Modus mit Webhook-URL (Mock-Server) uploaden, auf Status-Update pollen, Ergebnis herunterladen, Webhook-Empfang verifizieren
> 3. Ungültiger Key → 401
> 4. Falsches Mime → 415
> 5. Datei > 100 MB → 413
> 
> Stelle Ollama-Aufrufe per Fixture auf einen Fake-Responder um, damit Tests ohne echtes Model laufen.

---

## 9. Deployment

### 9.1 Minimaler Server (MVP)
- Ein Host (Hetzner / eigener Server) mit Docker
- `docker compose up -d` startet API, Worker, Redis
- Ollama läuft auf demselben Host (systemd-Service) mit gezogenem `glm-ocr`-Modell — **nicht in Docker**, um GPU-Zugriff und Performance zu maximieren. `OLLAMA_URL=http://host.docker.internal:11434` im Container
- Caddy davor als Reverse-Proxy mit automatischem HTTPS
- SQLite-Datei liegt in `./data/ocr.db`, wird per nächtlichem Cron nach `./backups/` gesichert

### 9.2 Erst-Einrichtung
```bash
git clone <repo>
cd ocr-api
cp .env.example .env          # Werte ausfüllen
python scripts/init_db.py     # DB + Admin anlegen
docker compose up -d
```

Login auf `https://ocr.deine-domain.de/admin/login`, Kunde + Key anlegen, fertig.

---

## 10. Abgrenzung & nicht-funktionale Anforderungen

- **Performance:** Sync-Endpunkt für Dokumente bis ~20 Seiten zumutbar. Größere PDFs sollten den Async-Weg nehmen.
- **Skalierung:** Mehr Worker = mehr parallele Jobs, bleibt aber durch die einzelne Ollama-Instanz serialisiert. Für echte Parallelität später mehrere Ollama-Instanzen + Load-Balancer.
- **Observability:** strukturierte JSON-Logs + Job-Historie in der DB sind MVP-Minimum. Prometheus-Metriken optional in Phase 10.
- **Wartbarkeit:** Jeder Service (Pipeline, Formatter, Webhook) hat eigene Tests. Kein Code-Sharing zwischen Admin-UI und API außer über `services/`.
- **Sicherheitskritisch:** API-Keys **niemals** loggen, auch nicht in Debug-Logs. Upload-Dateinamen sanitizen vor Storage.

---

## Anhang A — `.env.example`

```env
# Database
DATABASE_URL=sqlite:////data/ocr.db

# Redis (for ARQ)
REDIS_URL=redis://redis:6379/0

# Ollama
OLLAMA_URL=http://host.docker.internal:11434
OLLAMA_MODEL=glm-ocr

# Admin
ADMIN_USERNAME=admin
ADMIN_PASSWORD_HASH=            # bcrypt hash, siehe scripts/hash_password.py
SESSION_SECRET=                 # 32+ random bytes

# Storage
STORAGE_DIR=/data/storage

# Misc
ALLOWED_ORIGINS=
MAX_UPLOAD_MB=100
RESULT_TTL_DAYS=30
```

## Anhang B — Beispiel-cURL

**Sync:**
```bash
curl -X POST https://ocr.example.com/v1/ocr \
  -H "X-API-Key: sk_live_xxx" \
  -F "file=@Cyber_VVG.pdf" \
  -F "output_format=md" \
  -F "mode=sync" \
  -o result.md
```

**Async mit Webhook:**
```bash
curl -X POST https://ocr.example.com/v1/ocr \
  -H "X-API-Key: sk_live_xxx" \
  -F "file=@Sach_Inhalt_VVG.pdf" \
  -F "output_format=toon" \
  -F "mode=async" \
  -F "webhook_url=https://my-app.com/webhooks/ocr"
```

---

## Anhang C — Pipeline v4 Referenz-Code

Dies ist der **vollständige, bewährte** v4-Pipeline-Code, der als Grundlage für `app/services/ocr_pipeline.py` dient. Die 13 Strategien, Split-Logik und Grayscale-Behandlung müssen 1:1 übernommen werden.

```python
#!/usr/bin/env python3
"""Pipeline v4: PDF → glm-ocr → MD + TOON — 100% Erfolgsquote durch Multi-Strategie-Fallback"""
import os, sys, json, base64, subprocess, requests, glob, time, re, tempfile
from PIL import Image

OLLAMA_URL = "http://localhost:11434/api/generate"
OCR_MODEL = "glm-ocr"
INPUT_DIR = os.path.expanduser("~/Downloads")
OUT_DIR = os.path.expanduser("~/Downloads")
PDFS = ["Cyber_VVG.pdf", "Sach_Inhalt_VVG.pdf"]

# ── helpers ──────────────────────────────────────────────────────────

def run_ocr(img_path):
    """Send image to glm-ocr, return text or raise on error."""
    with open(img_path, "rb") as f:
        b64 = base64.b64encode(f.read()).decode()
    r = requests.post(OLLAMA_URL, json={
        "model": OCR_MODEL,
        "prompt": "OCR",
        "images": [b64],
        "stream": False
    }, timeout=120)
    r.raise_for_status()
    return r.json().get("response", "")


def extract_page_image(pdf_path, page_num, dpi, tmp_dir):
    """Extract single page via pdftoppm, return image path."""
    prefix = os.path.join(tmp_dir, f"pg{page_num}")
    subprocess.run([
        "pdftoppm", "-r", str(dpi), "-jpeg", "-f", str(page_num), "-l", str(page_num),
        pdf_path, prefix
    ], capture_output=True, timeout=30)
    candidates = glob.glob(f"{prefix}*.jpg")
    return candidates[0] if candidates else None


def resize_with_sips(img_path, max_px):
    """Resize in-place via sips (keeps aspect ratio). HINWEIS: sips ist macOS-only —
    auf Linux durch Pillow-Resize ersetzen."""
    subprocess.run(
        ["sips", "--resampleHeightWidthMax", str(max_px), img_path],
        capture_output=True, timeout=15
    )


def to_grayscale_jpeg(src_path, dst_path, max_px=None, quality=75):
    """Convert to grayscale JPEG via Pillow, optionally resize."""
    img = Image.open(src_path).convert("L")  # grayscale
    if max_px:
        w, h = img.size
        ratio = min(max_px / w, max_px / h, 1.0)
        if ratio < 1.0:
            img = img.resize((int(w * ratio), int(h * ratio)), Image.LANCZOS)
    img.save(dst_path, "JPEG", quality=quality)
    return dst_path


def to_compressed_jpeg(src_path, dst_path, max_px=400, quality=50):
    """Heavy compression + small size."""
    img = Image.open(src_path).convert("RGB")
    w, h = img.size
    ratio = min(max_px / w, max_px / h, 1.0)
    if ratio < 1.0:
        img = img.resize((int(w * ratio), int(h * ratio)), Image.LANCZOS)
    img.save(dst_path, "JPEG", quality=quality)
    return dst_path


def split_page_halves(src_path, tmp_dir, page_num):
    """Split image into top and bottom halves."""
    img = Image.open(src_path)
    w, h = img.size
    mid = h // 2
    top = img.crop((0, 0, w, mid))
    bot = img.crop((0, mid, w, h))
    top_path = os.path.join(tmp_dir, f"pg{page_num}_top.jpg")
    bot_path = os.path.join(tmp_dir, f"pg{page_num}_bot.jpg")
    top.save(top_path, "JPEG", quality=80)
    bot.save(bot_path, "JPEG", quality=80)
    return top_path, bot_path


def try_ocr(img_path, label=""):
    """Try OCR, return (text, True) or ("", False)."""
    try:
        t0 = time.time()
        text = run_ocr(img_path)
        elapsed = time.time() - t0
        if text.strip():
            return text.strip(), True, elapsed
        return "", False, 0
    except Exception:
        return "", False, 0


def get_page_count(pdf_path):
    r = subprocess.run(["pdfinfo", pdf_path], capture_output=True, text=True, timeout=10)
    for line in r.stdout.splitlines():
        if line.startswith("Pages:"):
            return int(line.split(":")[1].strip())
    return 0


# ── Strategies ───────────────────────────────────────────────────────

STRATEGIES = [
    # (name, dpi, max_px, grayscale, quality, split)
    ("150dpi/1024px",          150, 1024, False, 85, False),
    ("100dpi/768px",           100,  768, False, 85, False),
    ("72dpi/512px",             72,  512, False, 80, False),
    ("150dpi/1024px/gray",     150, 1024, True,  85, False),
    ("100dpi/768px/gray",      100,  768, True,  80, False),
    ("72dpi/512px/gray",        72,  512, True,  75, False),
    ("100dpi/400px/compress",  100,  400, False, 50, False),
    ("72dpi/400px/gray/comp",   72,  400, True,  50, False),
    ("150dpi/split",           150, 1024, False, 80, True),
    ("100dpi/split/gray",      100,  768, True,  75, True),
    ("72dpi/300px/gray/comp",   72,  300, True,  40, False),
    ("150dpi/600px",           150,  600, False, 80, False),
    ("100dpi/600px/gray",      100,  600, True,  75, False),
]


def ocr_page(pdf_path, page_num, tmp_dir):
    """Try all strategies until one succeeds. Returns (text, strategy_name, elapsed)."""

    for name, dpi, max_px, gray, quality, split in STRATEGIES:

        # 1) Extract page image
        img_path = extract_page_image(pdf_path, page_num, dpi, tmp_dir)
        if not img_path:
            continue

        if not split:
            # Resize
            resize_with_sips(img_path, max_px)

            # Apply grayscale / compression if needed
            if gray or quality < 80:
                proc_path = os.path.join(tmp_dir, f"pg{page_num}_proc.jpg")
                if gray:
                    to_grayscale_jpeg(img_path, proc_path, max_px=max_px, quality=quality)
                else:
                    to_compressed_jpeg(img_path, proc_path, max_px=max_px, quality=quality)
                target = proc_path
            else:
                target = img_path

            text, ok, elapsed = try_ocr(target)

            # Cleanup
            for f in glob.glob(os.path.join(tmp_dir, f"pg{page_num}*")):
                try: os.remove(f)
                except: pass

            if ok:
                return text, name, elapsed

        else:
            # Split strategy: OCR top half + bottom half separately
            resize_with_sips(img_path, max_px)

            if gray:
                gray_path = os.path.join(tmp_dir, f"pg{page_num}_gray.jpg")
                to_grayscale_jpeg(img_path, gray_path, max_px=max_px, quality=quality)
                source = gray_path
            else:
                source = img_path

            top_path, bot_path = split_page_halves(source, tmp_dir, page_num)

            top_text, top_ok, _ = try_ocr(top_path)
            bot_text, bot_ok, _ = try_ocr(bot_path)

            # Cleanup
            for f in glob.glob(os.path.join(tmp_dir, f"pg{page_num}*")):
                try: os.remove(f)
                except: pass

            if top_ok or bot_ok:
                combined = ""
                if top_ok:
                    combined += top_text
                if bot_ok:
                    combined += "\n" + bot_text
                return combined.strip(), name, 0

    return None, "ALLE_FEHLGESCHLAGEN", 0


# ── TOON conversion ─────────────────────────────────────────────────

def text_to_toon(title, pages_text):
    lines = []
    lines.append("document:")
    lines.append(f"  title: {title}")
    lines.append("  type: legal_document")
    lines.append(f"  pages: {len(pages_text)}")
    for pg_num, text in sorted(pages_text.items()):
        lines.append(f"page[{pg_num}]:")
        if text is None:
            lines.append("  text: [OCR-Fehler]")
            continue
        sections = re.split(r'(§\s*\d+)', text)
        if len(sections) > 1:
            i = 0
            while i < len(sections):
                part = sections[i].strip()
                if re.match(r'§\s*\d+', part) and i + 1 < len(sections):
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
    return "\n".join(lines)


# ── Main pipeline ────────────────────────────────────────────────────

def process_pdf(pdf_name):
    pdf_path = os.path.join(INPUT_DIR, pdf_name)
    if not os.path.exists(pdf_path):
        print(f"FEHLER: {pdf_path} nicht gefunden!")
        return

    page_count = get_page_count(pdf_path)
    base = os.path.splitext(pdf_name)[0]

    print("=" * 60)
    print(f"Processing: {pdf_name} ({page_count} Seiten)")
    print("=" * 60)
    sys.stdout.flush()

    pages_text = {}
    ok_count = 0
    fail_count = 0

    with tempfile.TemporaryDirectory(prefix=f"vvg_{base}_") as tmp_dir:
        for pg in range(1, page_count + 1):
            text, strategy, elapsed = ocr_page(pdf_path, pg, tmp_dir)
            if text:
                preview = text[:80].replace("\n", " ")
                print(f"  Seite {pg}/{page_count}: OK ({elapsed:.1f}s, {strategy}) - {preview}...")
                pages_text[pg] = text
                ok_count += 1
            else:
                print(f"  Seite {pg}/{page_count}: FEHLER nach {len(STRATEGIES)} Strategien")
                pages_text[pg] = None
                fail_count += 1
            sys.stdout.flush()

    # Write MD
    md_path = os.path.join(OUT_DIR, f"{base}.md")
    with open(md_path, "w") as f:
        f.write(f"# {base}\n\n")
        for pg in range(1, page_count + 1):
            f.write(f"## Seite {pg}\n\n")
            if pages_text[pg]:
                f.write(pages_text[pg] + "\n\n")
            else:
                f.write(f"[OCR-Fehler auf Seite {pg} — alle {len(STRATEGIES)} Strategien fehlgeschlagen]\n\n")

    # Write TOON
    toon_path = os.path.join(OUT_DIR, f"{base}.toon")
    with open(toon_path, "w") as f:
        f.write(text_to_toon(base, pages_text))

    print(f"\nOCR fertig: {ok_count} OK, {fail_count} Fehler")
    print(f"  MD:   {md_path}")
    print(f"  TOON: {toon_path}")


if __name__ == "__main__":
    for pdf in PDFS:
        process_pdf(pdf)
```

### Wichtige Hinweise zur Portierung in die API

1. **`sips` ist macOS-only** — im Docker-Container (Linux) muss `resize_with_sips` durch eine reine Pillow-Variante ersetzt werden: `Image.open(path).thumbnail((max_px, max_px), Image.LANCZOS)` und wieder speichern.
2. **Globale Konstanten** (`OLLAMA_URL`, `OCR_MODEL`, `INPUT_DIR`, `OUT_DIR`, `PDFS`) fallen weg — werden durch `Settings`-Injection und Funktionsparameter ersetzt.
3. **`process_pdf`** wird in `run_ocr(input_path, tmp_dir)` umgebaut und gibt ein `OcrResult`-Dataclass zurück statt direkt Dateien zu schreiben (das Schreiben übernimmt `formatters.py`).
4. **Bild-Input unterstützen:** Wenn der Input schon ein Bild ist, `extract_page_image` überspringen und die Strategien direkt auf das Bild anwenden (Resize/Grayscale/Split funktionieren identisch).
5. **Subprozess-Isolation:** Im ARQ-Worker die Pipeline in `subprocess.run([sys.executable, "-m", "app.services.ocr_runner", ...])` laufen lassen, damit Ollama-Crashes den Worker nicht mitreißen.
6. **TOON-Formatter:** `text_to_toon` wandert unverändert in `formatters.py` als `_format_toon`.