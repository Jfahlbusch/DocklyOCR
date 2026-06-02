# DocklyOCR — Arbeitsstand 2026-04-17 / Plan für 2026-04-18

## Stand heute (Prod)

- **Branch:** `main`, Prod auf `efd5220` (API-Server `51.15.129.221`, GPU `DocklyOCR` fr-par-2)
- **Backend:** vLLM + Qwen2.5-VL-7B (Ollama komplett raus, Commit `0dee1da`)
- **Tests:** 143 grün, Ruff clean

### Heute deployed
| Commit | Inhalt |
|---|---|
| `0dee1da` | refactor: Ollama entfernt, `OLLAMA_*` → `BACKEND_*`, Dead-Code weg, `auto-shutdown.sh` auf vLLM umgestellt |
| `efd5220` | fix: Inferenz-Smoketest in `ensure_gpu_running()`, Logging in `try_ocr`, Worker markiert Job `failed` bei `pages_ok==0` |

## Hypothese zum 0-pages-OK-Bug

`_backend_ready()` bekam 200 von `/v1/models`, aber vLLM war in CUDA-Graph-Compile. Die 12 parallelen Pipeline-Requests landeten in der Warmup-Phase und alle wurden mit 500 beantwortet. Fix: zusätzlicher Smoketest-POST (8×8 JPEG) bevor `ensure_gpu_running()` zurückkehrt.

## Morgen — Reihenfolge

### 1. Fix verifizieren (zuerst!)
- 5×PDF-Batch auf Prod hochladen (GPU ist aus → Cold-Start-Szenario)
- Prüfen: `/v1/jobs/{id}` → alle `done` mit `pages_ok > 0`
- Bei Fehler: `ssh root@51.15.129.221 "docker compose -f /opt/dockly-ocr/docker-compose.yml logs worker --tail 200"` — neue Warn-Logs aus `try_ocr` zeigen jetzt den echten Backend-Fehler

### 2. Offene Fehlerquellen aus Bestandsaufnahme
Aus dem Prozessdiagramm noch nicht adressiert:

**#3 — Circuit-Breaker bei Massenausfall**
- Wenn in den ersten ~3 Seiten alle Strategien fehlschlagen, aktuell trotzdem parallel weitergefeuert gegen 40+ weitere Seiten
- Vorschlag: nach `failed_count >= 3 && ok_count == 0` Pipeline mit `RuntimeError("Backend liefert nur Fehler")` abbrechen → Worker markiert `failed` (greift jetzt schon mit #5-Fix)
- Datei: `app/services/ocr_pipeline.py`, `run_ocr()` innerhalb der `as_completed`-Schleife

**#7 — `BACKEND_REQUEST_TIMEOUT_S` auf Prod nur 60s**
- Prod `.env` hat noch 60s (aus alter Ollama-Zeit). `.env.example` Default ist 120s
- Fix: `ssh root@51.15.129.221 "sed -i 's/^BACKEND_REQUEST_TIMEOUT_S=60/BACKEND_REQUEST_TIMEOUT_S=120/' /opt/dockly-ocr/.env && docker compose -f /opt/dockly-ocr/docker-compose.yml up -d"`

**Optional — Sync-Pfad auch absichern**
- `app/routers/ocr.py::_run_sync` hat noch die alte Logik: `status=done` auch bei `pages_ok==0`. Spiegeln zum Worker-Verhalten. Wenn wir sync weiter unterstützen wollen.

**Optional — stale Doc**
- `app/routers/ocr.py:6` Docstring sagt noch „13-strategy pipeline" — sind nur 5. Ein-Zeilen-Fix.

### 3. Wenn alles stabil
- Monitoring: einen Blick auf die vLLM `/metrics` vom API-Host nehmen (num_requests_running, ttft, generation_time) — gibt uns zukünftig objektive Latenz-Daten
- Vielleicht ein `GET /v1/metrics` proxy-Endpoint im Admin-Bereich?

## Nützliche Commands

```bash
# Server-Status
ssh root@51.15.129.221 "docker compose -f /opt/dockly-ocr/docker-compose.yml ps && curl -s localhost:8000/health"

# Worker-Logs live
ssh root@51.15.129.221 "docker compose -f /opt/dockly-ocr/docker-compose.yml logs -f worker"

# GPU manuell an/aus
scw instance server start  server-id=<GPU_ID> zone=fr-par-2
scw instance server stop   server-id=<GPU_ID> zone=fr-par-2

# Deploy nach commit
git push && ssh root@51.15.129.221 "cd /opt/dockly-ocr && git pull && docker compose up -d --build"

# Tests lokal
.venv/bin/pytest tests/ -q
.venv/bin/ruff check app/ tests/
```

## Offene Fragen für morgen
- Gibt es ein bevorzugtes Test-PDF für die Verifikation? (Schrader.pdf vom letzten Versuch?)
- Sync-Pfad behalten oder als deprecated markieren? (die meisten Clients nutzen async mit Webhook)
