# DocklyOCR — Implementation Design

**Datum:** 2026-04-15
**Status:** Approved for implementation
**Source-Spec:** [`OCR-API-Projekt-Anforderungen.md`](../../../OCR-API-Projekt-Anforderungen.md) — bleibt die maßgebliche Anforderungsquelle. Dieses Doc ergänzt sie um Ausführungs- und technische Deltas.

---

## 1. Zweck dieses Dokuments

`OCR-API-Projekt-Anforderungen.md` ist bereits ein vollständiger Functional-Spec (Datenmodell, API-Endpunkte, Pipeline-Code in Anhang C, Phase-by-Phase-Prompts). Dieses Design-Doc beantwortet nur die offenen Fragen der **Umsetzung**:

1. Wie wird der Build in einer Session parallelisiert?
2. Welche technischen Entscheidungen weichen bewusst vom Spec-Wording ab?
3. Wie sieht die Admin-UI konkret aus?
4. Was wird bewusst **nicht** in diesem Durchlauf gebaut?

---

## 2. Ausführungsplan (Parallel-Agent-Orchestrierung)

Der Build erfolgt in vier Wellen. Innerhalb einer Welle arbeiten mehrere Agents parallel, zwischen den Wellen gibt es harte Dependencies.

### Welle 0 — Scaffold (sequentiell, ~5 Min)

Von Claude selbst ausgeführt:
- `pyproject.toml` (uv-kompatibel) mit allen Dependencies
- Vollständige Ordnerstruktur (`app/`, `app/routers/`, `app/services/`, `app/workers/`, `app/templates/`, `tests/`, `scripts/`)
- `.env.example` mit allen benötigten Variablen
- `Dockerfile` (Python 3.11-slim + `poppler-utils`)
- `docker-compose.yml` mit Services `api`, `worker`, `redis`
- Minimales `app/main.py` + `app/config.py` (pydantic-settings)
- `README.md` (Stub)
- `.gitignore`

**Exit-Kriterium:** `uv run python -c "from app.main import app"` funktioniert.

### Welle 1 — Foundation (2 parallele Agents)

| Agent-Name | Umfang | Kernschnittstellen nach außen |
|---|---|---|
| **backend-foundation** | `app/models.py` (Customer, ApiKey, Job, AdminUser), `app/db.py` (engine, `get_session`, `init_db`), `app/auth.py` (`generate_api_key`, `require_api_key`-Dependency), `scripts/init_db.py`, `scripts/hash_password.py`, `tests/test_models.py`, `tests/test_auth.py` | SQLModel-Klassen, `get_session` Dependency, `require_api_key` Dependency |
| **ocr-core** | `app/services/ocr_pipeline.py` (13-Strategien-Port), `app/services/ocr_runner.py` (Subprozess-CLI), `app/services/formatters.py` (MD/TXT/TOON/JSON), `tests/test_pipeline.py` (mit Ollama-Mock), `tests/test_formatters.py` (Snapshot-Style) | `OcrResult`-Dataclass, `run_ocr(input_path, tmp_dir) -> OcrResult`, `format_output(result, fmt) -> (bytes, mime)` |

**Parallelisierbar**, weil Pipeline und DB sich gegenseitig nicht kennen. Beide Agents lesen nur `app/config.py`.

### Welle 2 — API-Layer & Admin-UI (2 parallele Agents)

| Agent-Name | Umfang | Dependencies |
|---|---|---|
| **api-endpoints** | `app/routers/ocr.py` (`POST /v1/ocr` sync+async), `app/routers/jobs.py` (`GET /v1/jobs/{id}`, `/result`, `GET /v1/jobs` paginated), `app/workers/ocr_worker.py` (ARQ-Task), `app/services/storage.py` (local FS mit S3-Interface), `app/services/webhook.py` (Delivery + Retry), Tests | Welle 1 (beide Agents) |
| **admin-ui** | `app/routers/admin.py` (alle Routen aus Spec §5.1), `app/templates/base.html`, `app/templates/admin/*.html` (login, dashboard, customers-list, customer-detail, key-create-modal, jobs-list), Session-Auth via `itsdangerous` | Welle 1 (backend-foundation für `AdminUser`) |

**Parallelisierbar**, weil API-Layer und Admin-UI nur über die bereits in Welle 1 gebauten Models und Auth-Funktionen kommunizieren — keine gegenseitigen Imports.

### Welle 3 — Polish & Verification (3 parallele Agents)

| Agent-Name | Umfang |
|---|---|
| **openapi-polish** | Alle Routen mit `summary`/`description`/`response_model`/`responses`-Beispielen versehen, `openapi_tags` im FastAPI-Konstruktor setzen |
| **e2e-tests** | `tests/test_e2e.py` (httpx.AsyncClient), Ollama-Fake-Fixture, Webhook-Mock-Server via `httpx.MockTransport`, `tests/fixtures/sample.pdf` (kleines 2-Seiten-PDF erzeugen) |
| **ci-readme** | `.github/workflows/ci.yml` (ruff + pytest), `README.md` ausbauen (Quickstart, cURL-Beispiele, Webhook-Payload-Doku) |

### Welle 4 — Launch & Verification (Claude selbst)

1. `ollama serve` sicherstellen (oder Hinweis an User)
2. `python scripts/init_db.py` (DB + Admin anlegen)
3. `docker compose up -d --build`
4. Warten auf `docker compose logs api` → `Application startup complete`
5. `curl http://localhost:8000/health` → `{"status":"ok",...}` validieren
6. Admin-UI im Browser öffnen, Login, Kunde + Key anlegen
7. `curl -X POST http://localhost:8000/v1/ocr -H "X-API-Key: …" -F file=@sample.pdf -F output_format=md -F mode=sync -o result.md` → funktionierende Markdown-Ausgabe
8. Dieselbe Datei im Async-Modus, Status pollen, Ergebnis herunterladen
9. **Nur dann** gilt das Projekt als gebaut

---

## 3. Technische Deltas zur Spec

### 3.1 `sips` → Pillow

Anhang C verwendet `sips --resampleHeightWidthMax`. `sips` ist macOS-only und im Debian-Container nicht verfügbar. Ersatz:

```python
def resize_image(img_path: Path, max_px: int) -> None:
    img = Image.open(img_path)
    img.thumbnail((max_px, max_px), Image.LANCZOS)
    img.save(img_path, "JPEG", quality=90)
```

Die `STRATEGIES`-Liste bleibt bit-identisch zum Spec-Anhang.

### 3.2 Subprozess-Isolation

Der ARQ-Worker führt die Pipeline **nie** im eigenen Prozess aus, sondern via:

```python
subprocess.run(
    [sys.executable, "-m", "app.services.ocr_runner",
     "--input", str(input_path),
     "--tmp-dir", str(tmp_dir),
     "--output-json", str(result_json_path)],
    timeout=1800,
    check=True,
)
```

`app/services/ocr_runner.py` ist ein schmaler CLI-Wrapper, der `run_ocr()` aufruft und das `OcrResult` als JSON serialisiert. Wenn Ollama crasht: Subprozess stirbt, Worker fängt `CalledProcessError`/`TimeoutExpired`, setzt Job-Status auf `failed`, lebt selbst weiter.

### 3.3 Ollama außerhalb des Containers

- Ollama läuft auf dem **Host** (GPU + bereits gezogenes Modell `glm-ocr:latest`)
- `docker-compose.yml` deklariert `extra_hosts: ["host.docker.internal:host-gateway"]` für Linux-Kompatibilität
- `.env`: `OLLAMA_URL=http://host.docker.internal:11434`
- `/health`-Endpunkt macht einen 2-Sekunden-Timeout-Ping auf Ollama und nennt im Response separat `ollama: "ok"` / `"unreachable"`
- **Für PRD:** Der User wird Ollama **nicht** installieren. Lösung: `OLLAMA_URL` zeigt dann auf einen externen Dienst (z. B. `http://ollama.interne-vpn:11434`). Der Code unterscheidet nicht — es ist ausschließlich eine ENV-Config.

### 3.4 Upload-Size-Limit

Eigene Starlette-Middleware `ContentLengthLimitMiddleware`:

```python
class ContentLengthLimitMiddleware:
    def __init__(self, app, max_bytes: int): ...
    async def __call__(self, scope, receive, send):
        if scope["type"] == "http":
            for name, value in scope["headers"]:
                if name == b"content-length" and int(value) > self.max_bytes:
                    # respond 413
                    ...
        await self.app(scope, receive, send)
```

Fällt auf Body-Reader zurück, wenn kein `Content-Length`-Header da ist (chunked).

### 3.5 Rate-Limiting — MVP

In-memory Token-Bucket pro `api_key.id`:

```python
class RateLimiter:
    def __init__(self, requests_per_minute: int = 10): ...
    async def check(self, key_id: int) -> RateLimitInfo: ...
```

Response-Header:
- `X-RateLimit-Limit: 10`
- `X-RateLimit-Remaining: 7`
- `X-RateLimit-Reset: 1744732800`

Bei Overflow: `429 Too Many Requests`. Redis-Backend ist vorbereitet (Interface), aber nicht aktiviert.

### 3.6 Webhook-HMAC-Signing

Feld `Customer.webhook_secret: str | None` existiert in der DB-Migration. Wenn gesetzt, wird der Header `X-Signature: sha256=<hex-hmac>` gesendet (HMAC-SHA256 über den JSON-Body). Wenn `None`: kein Header. Keine Self-Service-UI für den Secret im MVP — wird später per Admin-UI hinzugefügt.

---

## 4. Admin-UI Design-Richtung

### 4.1 Sprache: "Operator Console"

Nicht Marketing-Dashboard, sondern Admin-Werkzeug. Dichte Datentabellen, ruhige Flächen, technische Ästhetik. Vorbild: Stripe-Dashboard (klassisch), Linear-Command-Menu, Railway-Admin.

### 4.2 Design-Tokens

| Token | Wert |
|---|---|
| Page Background | `bg-slate-50` |
| Surface (Card) | `bg-white` mit `border border-slate-200` |
| Text primär | `text-slate-900` |
| Text sekundär | `text-slate-600` |
| Primär-Action | `bg-indigo-600 hover:bg-indigo-700 text-white` |
| Link | `text-indigo-600 hover:text-indigo-700` |
| Status: done | Dot `bg-emerald-500` |
| Status: processing/pending | Dot `bg-amber-500` |
| Status: failed | Dot `bg-rose-500` |
| Radius | `rounded-md` (6px) — global, nichts größer |
| Shadow | `shadow-sm` nur auf Cards |
| Font | System-Stack (kein Custom-Font → kein Build-Step) |
| Spacing-Rhythmus | 4 / 8 / 16 / 24 / 32 |

### 4.3 Layout

```
┌──────────────────────────────────────────────────┐
│ DocklyOCR      Dashboard  Customers  Jobs    [↪] │   ← 56px top-bar
├──────────────────────────────────────────────────┤
│                                                   │
│  Page Title                   [Primary Action]   │
│                                                   │
│  ┌─ Card ────────────────────────────────────┐   │
│  │  dense table / form                         │   │
│  └─────────────────────────────────────────────┘   │
│                                                   │
└──────────────────────────────────────────────────┘
```

- **Kein Sidebar** — nur Top-Bar mit 4 Nav-Items (`Dashboard`, `Customers`, `Jobs`, Logout)
- **Max-Width:** 1280px, zentriert
- **Keine Hero-Sections**, keine Dekoration, keine Gradients

### 4.4 Kernseiten

| Seite | Besonderheit |
|---|---|
| `GET /admin/login` | Zentrierte 400px-Card auf `slate-50`, keine Illustration |
| `GET /admin` (Dashboard) | 4 Stat-Cards in Grid (Kunden, Jobs heute/Woche/Monat) + Tabelle "Letzte 10 Jobs" |
| `GET /admin/customers` | Dense Table (Name, Email, Plan, Aktive Keys, Erstellt), Zeilen als Links, "+ Neuer Kunde" Ghost-Button oben rechts |
| `GET /admin/customers/{id}` | Header mit Customer-Metadaten + Sektion "API-Keys" + Sektion "Letzte Jobs" |
| Key-Create-Modal | HTMX-Modal, Klartext-Key in `<pre>` + Copy-Button, rote Warnzeile "Wird nur einmal angezeigt" |
| `GET /admin/jobs` | Filter-Bar (Status + Customer-Dropdown via HTMX) → Tabelle mit monospace `job_id`, Status-Dot, Seitenzahl |

### 4.5 HTMX-Interaktionen

- Formular-Submits → `hx-post` → Server liefert HTML-Fragment → Target-Swap
- Key-Modal → `hx-get /admin/customers/{id}/keys/new` → Inline-Modal-Swap
- Jobs-Filter → `hx-get /admin/jobs?status=done` → Tabellenbody-Swap
- Copy-Button → 5-Zeilen Inline-JS mit `navigator.clipboard`

### 4.6 Skill-Einsatz

Der `admin-ui`-Agent bekommt:
- Diese Design-Tokens als Hardcode-Constraints
- Eine **Referenz-Seite von Claude vorgeschrieben** (`dashboard.html` oder `customers-list.html`), damit das Muster klar ist
- Hinweis auf `frontend-design`-Skill für **Prinzipien** (Distinctive, non-generic, polish), nicht für Code-Generatoren — das Skill ist primär React/Next-orientiert, passt nicht 1:1 auf Jinja, aber die Qualitätsrichtlinien gelten

---

## 5. Explizit out-of-scope für diesen Durchlauf

- ❌ Automatisches 30-Tage-Cleanup alter Ergebnisse → Script vorbereitet (`scripts/cleanup_old_results.py`), aber kein Cron-Setup
- ❌ Prometheus-Metriken → Phase 10 / später
- ❌ S3-Storage → Interface in `storage.py` vorhanden, Implementation bleibt lokal
- ❌ CI-Matrix / Test-Caching → einfacher CI-Workflow (ruff + pytest on push)
- ❌ Self-Service-Signup → Admin legt Kunden manuell an (spec-konform)
- ❌ Stripe / Usage-Tracking / Paid-Plans → Architektur ist vorbereitet (Feld `Customer.plan`), aber nichts implementiert
- ❌ HTTPS / Caddy-Reverse-Proxy → wird nur im README dokumentiert, nicht gebaut

---

## 6. Risiken & Mitigation

| Risiko | Wahrscheinlichkeit | Mitigation |
|---|---|---|
| Ollama-Server läuft nicht beim Launch | hoch | Pre-flight check im `/health`-Endpunkt + klare Fehlermeldung in Welle 4 |
| Parallel-Agents generieren inkonsistente Imports | mittel | Welle 0 definiert `app/config.py`-Settings und `app/db.py`-Session-Schnittstelle vor; alle späteren Agents bekommen diese als **Vertrag** in ihrem Prompt |
| `glm-ocr` liefert leere Response auf Test-PDF | mittel | Welle 4 hat einen Fallback-Test mit generiertem Dummy-PDF; Abbruch mit klarer Fehlermeldung wenn alle 13 Strategien failen |
| SQLite-File in Docker-Volume nicht schreibbar | niedrig | `docker-compose.yml` mapped `./data` als named volume, Permissions via `chmod` im Dockerfile |
| Python 3.13 lokal vs. 3.11 im Container | niedrig | `pyproject.toml` spezifiziert `requires-python = ">=3.11"`, lokaler Dev nutzt uv-venv mit 3.11 wenn nötig — aber Docker-Build ist die Source-of-Truth |

---

## 7. Definition of Done

Das Projekt ist fertig, wenn:

1. `docker compose up -d --build` startet alle Services ohne Fehler
2. `GET /health` antwortet mit `{"status":"ok","ollama":"ok","db":"ok"}`
3. Admin kann sich einloggen, Kunde + API-Key anlegen, Klartext-Key sehen
4. `POST /v1/ocr` im sync-Modus mit einem Test-PDF liefert valides Markdown zurück
5. `POST /v1/ocr` im async-Modus mit Webhook-URL liefert nach Verarbeitung eine Webhook-POST-Delivery
6. `GET /v1/jobs/{id}` und `GET /v1/jobs/{id}/result` funktionieren
7. `pytest` läuft durch (alle Tests grün)
8. `/docs` (Swagger) zeigt alle Endpunkte mit Beschreibungen
9. `README.md` enthält Quickstart und cURL-Beispiele

---

## 8. Offene Punkte (User-Entscheidung offen)

Keine. Alle drei Design-Abschnitte wurden am 2026-04-15 vom User bestätigt.
