# DocklyOCR

Self-hosted OCR API powered by **Qwen2.5-VL via vLLM**, with a multi-strategy fallback pipeline (column-split detection, re-scans, table re-prompt), API-key authentication, webhook delivery, batch upload, and a minimal admin UI.

[![CI](https://github.com/Jfahlbusch/DocklyOCR/actions/workflows/ci.yml/badge.svg)](https://github.com/Jfahlbusch/DocklyOCR/actions)

## Features

- **PDF and image input** (JPG, PNG, TIFF) up to 100 MB
- **Batch upload**: multiple files per request via `/v1/ocr/batch`
- **Multi-strategy pipeline**: parallel page OCR, column-split for multi-column layouts, context-aware merge across page boundaries
- **Output formats**: Markdown, plain text, TOON, JSON
- **Sync or async delivery** (webhook + polling)
- **API-key authentication**, prefix-visible and hash-stored
- **Rate limiting** (10 req/min per key by default)
- **Admin UI** for managing customers, API keys, jobs (stop / restart / delete)
- **On-demand GPU**: auto-start/stop Scaleway GPU instances
- Self-hostable via `docker compose up`
- OpenAPI docs at `/docs`, `/redoc`, and `/scalar`

## Architecture at a Glance

```
         ┌─────────────────────────┐        ┌─────────────────────────┐
         │  DEV1-M / CPU instance  │        │  GPU instance (on-demand)│
         │  (always on)            │        │                         │
Clients ▶│  Caddy → FastAPI        │──────▶│  vLLM + Qwen2.5-VL-7B   │
         │  ARQ worker             │        │  (/v1/chat/completions) │
         │  Redis (queue)          │        │                         │
         │  SQLite (metadata)      │        │  OR  Ollama + glm-ocr   │
         └─────────────────────────┘        └─────────────────────────┘
                                             ▲ auto-start via SCW API
                                             ▼ auto-stop when idle
```

Two supported OCR backends, selected by `OLLAMA_USE_OPENAI_API`:

| Backend | Use when | Model | Endpoint |
|---|---|---|---|
| **vLLM** (recommended for GPU) | H100 / L4 GPU available | Qwen2.5-VL-7B-Instruct | `/v1/chat/completions` |
| **Ollama** | CPU-only or simple local dev | `glm-ocr` | `/api/generate` |

## Prerequisites

| Component | Notes |
|---|---|
| Docker + docker-compose | 24+ |
| OCR backend | Either vLLM (Docker image `vllm/vllm-openai:latest`) or Ollama on host with `glm-ocr` pulled |
| Python (dev only) | 3.11 for running tests locally |

## Quickstart (vLLM backend)

```bash
# 1. Clone and configure
git clone https://github.com/Jfahlbusch/DocklyOCR.git dockly-ocr
cd dockly-ocr
cp .env.example .env

# 2. Set the backend URL + credentials in .env
#    OLLAMA_URL=http://<gpu-host>:8000
#    OLLAMA_MODEL=qwen2.5-vl-7b
#    OLLAMA_USE_OPENAI_API=true
#
#    Generate admin password hash + session secret:
python scripts/hash_password.py 'your-strong-password'  # -> ADMIN_PASSWORD_HASH (escape $ as $$)
python -c "import secrets; print(secrets.token_urlsafe(48))"  # -> SESSION_SECRET

# 3. Initialize DB (from inside the API container after first up)
docker compose up -d --build
docker compose exec api python scripts/init_db.py

# 4. Verify
curl http://localhost:8000/health
# {"status":"ok","ollama":"ok","db":"ok"}
```

Endpoints:

- API: http://localhost:8000
- **Scalar**: http://localhost:8000/scalar _(recommended — multi-file upload works)_
- Swagger: http://localhost:8000/docs _(array-file upload is broken)_
- ReDoc: http://localhost:8000/redoc _(read-only docs)_
- Admin: http://localhost:8000/admin

For the full production deployment on Scaleway (H100-1-80G with auto start/stop), see [`SCALEWAY_SETUP.md`](SCALEWAY_SETUP.md).

## First Use

1. Open `/admin` and log in with the credentials from `.env`
2. Click **+ New Customer** and fill in name + email
3. Open the customer and click **+ New Key** — copy the plaintext key immediately (shown once)
4. Run your first OCR:

```bash
export KEY="sk_live_xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx"

curl -X POST http://localhost:8000/v1/ocr \
  -H "X-API-Key: $KEY" \
  -F "file=@document.pdf" \
  -F "output_format=md" \
  -F "mode=sync" \
  -o result.md
```

## API Examples

### Single file — synchronous

```bash
curl -X POST http://localhost:8000/v1/ocr \
  -H "X-API-Key: sk_live_xxx" \
  -F "file=@document.pdf" \
  -F "output_format=md" \
  -F "mode=sync" \
  -o result.md
```

Returns `200 OK` with the rendered body. Blocks up to `SYNC_TIMEOUT_S` (default 300 s).

### Single file — asynchronous with webhook

```bash
curl -X POST http://localhost:8000/v1/ocr \
  -H "X-API-Key: sk_live_xxx" \
  -F "file=@document.pdf" \
  -F "output_format=json" \
  -F "mode=async" \
  -F "webhook_url=https://your-app.com/webhooks/ocr"
```

`202 Accepted`:
```json
{
  "job_id": "7c9e6f8d5b2a4e1c9d8f3a6b7e5c2d1a",
  "status": "pending",
  "status_url": "/v1/jobs/7c9e6f8d5b2a4e1c9d8f3a6b7e5c2d1a"
}
```

When done, DocklyOCR `POST`s to the webhook URL with the result URL and metadata. If `customer.webhook_secret` is set, the request carries `X-Signature: sha256=<hex>` (HMAC-SHA256 of the body).

### Batch — multiple files in one request

```bash
curl -X POST http://localhost:8000/v1/ocr/batch \
  -H "X-API-Key: sk_live_xxx" \
  -F "files=@doc1.pdf" \
  -F "files=@doc2.pdf" \
  -F "files=@doc3.pdf" \
  -F "output_format=md" \
  -F "webhook_url=https://your-app.com/webhooks/ocr"
```

`202 Accepted` with a list of job IDs — each pollable individually via `/v1/jobs/{id}`.

### Polling and downloading

```bash
# Status
curl -H "X-API-Key: sk_live_xxx" http://localhost:8000/v1/jobs/<job_id>

# Result
curl -H "X-API-Key: sk_live_xxx" http://localhost:8000/v1/jobs/<job_id>/result -o result.json
```

## Output Formats

| Format | MIME | Use case |
|---|---|---|
| `md` | `text/markdown` | human-readable; `## Seite N` headings + per-page strategy footnote |
| `txt` | `text/plain` | raw text, pages separated by `\f` |
| `toon` | `application/x-toon` | structured legal-document format with `§`-section detection |
| `json` | `application/json` | full metadata: per-page text, strategy, elapsed time, meta counts, `is_table` |

## Rate Limiting

10 requests per minute per API key by default (`RATE_LIMIT_PER_MINUTE`). Over-limit responses return `429 Too Many Requests` with `Retry-After` and `X-RateLimit-*` headers.

## Deployment Notes

- **Reverse proxy:** put Caddy or Nginx in front of port 8000 for TLS. Example Caddyfile:
  ```caddy
  ocr.example.com {
    reverse_proxy localhost:8000
  }
  ```
  Expose only the API port — not the OCR backend's port (vLLM 8000 or Ollama 11434).
- **Backups:** SQLite DB at `./data/ocr.db` — nightly `cp` is enough with WAL mode.
- **Cleanup:** the daily cleanup cron (see `scripts/cleanup_old_results.py`) deletes jobs older than `RESULT_TTL_DAYS` (default 30, set to 7 on the Scaleway setup).
- **Scaling:** with vLLM the backend handles multi-request concurrency natively. Beyond a single GPU, put several backend instances behind a load balancer.

## Development

```bash
uv venv --python 3.11 .venv
uv pip install --python .venv/bin/python -e ".[dev]"

.venv/bin/pytest                                    # run tests (142)
.venv/bin/ruff check app/                           # lint
.venv/bin/ruff format app/                          # format

.venv/bin/uvicorn app.main:app --reload --port 8000 # dev server (no docker)
```

For the dev server without docker you still need:
- Redis (`docker run -d -p 6379:6379 redis:7-alpine`)
- An OCR backend (either vLLM on a GPU or Ollama with `glm-ocr`)
- `.env` adjusted to your local ports and `DATABASE_URL=sqlite:///./data/ocr.db`

## Architecture Docs

- [`SCALEWAY_SETUP.md`](SCALEWAY_SETUP.md) — production deploy on Scaleway (H100 + on-demand shutdown)
- [`docs/superpowers/specs/2026-04-15-docklyocr-implementation-design.md`](docs/superpowers/specs/2026-04-15-docklyocr-implementation-design.md) — original design
- [`docs/superpowers/specs/2026-04-16-pipeline-v5-chunked-table-design.md`](docs/superpowers/specs/2026-04-16-pipeline-v5-chunked-table-design.md) — pipeline v5 (batch extract, table detection, merge)
- [`OCR-API-Projekt-Anforderungen.md`](OCR-API-Projekt-Anforderungen.md) — original functional spec (German)

## License

Proprietary — Hylab.
