# DocklyOCR

Self-hosted OCR API based on `glm-ocr` (Ollama) with a 13-strategy multi-fallback pipeline, API-key authentication, webhook delivery, and a minimal admin UI.

[![CI](https://github.com/.../actions/workflows/ci.yml/badge.svg)](https://github.com/.../actions) <!-- stub, replace with actual URL -->

## Features

- PDF and image input (JPG, PNG, TIFF) up to 100 MB
- 13-strategy fallback pipeline — bewaehrt auf juristischen Dokumenten mit 100 % Erfolgsquote
- Output formats: Markdown, plain text, TOON, JSON
- Synchronous or asynchronous (webhook) delivery
- API-key authentication with prefix-visible, hash-stored keys
- In-memory rate limiting (10 req/min per key by default)
- Admin UI for managing customers, API keys, and viewing jobs
- Self-hostable via `docker compose up`
- Swagger/OpenAPI docs at `/docs`

## Prerequisites

| Component | Version | Notes |
|---|---|---|
| Docker + docker-compose | 24+ | for `api` + `worker` + `redis` services |
| Ollama | latest | **runs on the host**, never in Docker |
| `glm-ocr` model | — | `ollama pull glm-ocr` (~2.2 GB) |
| Python (dev only) | 3.11 | only for running tests locally |

> **Ollama is never exposed externally.** Bind it to `127.0.0.1:11434` (default). The DocklyOCR API container reaches it via `host.docker.internal:11434`. Your reverse proxy (e.g. Caddy) must ONLY forward port 8000 (the API) to the internet — port 11434 should never leave the host.

## Quickstart

```bash
# 1. Clone and configure
git clone <repo-url> dockly-ocr
cd dockly-ocr
cp .env.example .env

# 2. Generate admin password hash and session secret, then paste into .env
python scripts/hash_password.py 'your-strong-password'      # copy output into ADMIN_PASSWORD_HASH
python -c "import secrets; print('SESSION_SECRET='+secrets.token_urlsafe(48))"

# 3. Make sure Ollama is running and glm-ocr is pulled
ollama serve &                         # or: systemctl enable --now ollama
ollama pull glm-ocr

# 4. Initialize DB and admin user
python scripts/init_db.py

# 5. Start the stack
docker compose up -d --build

# 6. Verify
curl http://localhost:8000/health
```

Endpoints:

- API: http://localhost:8000
- Swagger: http://localhost:8000/docs
- ReDoc: http://localhost:8000/redoc
- Admin: http://localhost:8000/admin

## First Use

1. Open http://localhost:8000/admin and log in with the credentials from `.env`
2. Click **+ New Customer** and fill in name + email
3. Open the customer and click **+ New Key**, copy the plaintext key immediately (shown once)
4. Test it:

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

### Synchronous OCR (blocking)

```bash
curl -X POST http://localhost:8000/v1/ocr \
  -H "X-API-Key: sk_live_xxx" \
  -F "file=@document.pdf" \
  -F "output_format=md" \
  -F "mode=sync" \
  -o result.md
```

Returns `200 OK` with `Content-Type: text/markdown; charset=utf-8` and the Markdown body. Blocks for up to 300 s (configurable via `SYNC_TIMEOUT_S`).

### Asynchronous OCR with Webhook

```bash
curl -X POST http://localhost:8000/v1/ocr \
  -H "X-API-Key: sk_live_xxx" \
  -F "file=@document.pdf" \
  -F "output_format=json" \
  -F "mode=async" \
  -F "webhook_url=https://your-app.com/webhooks/ocr"
```

Returns `202 Accepted`:

```json
{
  "job_id": "7c9e6f8d5b2a4e1c9d8f3a6b7e5c2d1a",
  "status": "pending",
  "status_url": "/v1/jobs/7c9e6f8d5b2a4e1c9d8f3a6b7e5c2d1a"
}
```

When done, DocklyOCR `POST`s to your webhook URL:

```json
{
  "job_id": "7c9e6f8d5b2a4e1c9d8f3a6b7e5c2d1a",
  "status": "done",
  "output_format": "json",
  "page_count": 43,
  "pages_ok": 43,
  "pages_failed": 0,
  "result_url": "/v1/jobs/7c9e6f8d5b2a4e1c9d8f3a6b7e5c2d1a/result",
  "finished_at": "2026-04-15T12:34:56Z"
}
```

If `customer.webhook_secret` is set, the request includes `X-Signature: sha256=<hex>` computed as `HMAC-SHA256(secret, body)`. Verify before trusting the payload.

### Polling job status

```bash
curl -H "X-API-Key: sk_live_xxx" \
  http://localhost:8000/v1/jobs/7c9e6f8d5b2a4e1c9d8f3a6b7e5c2d1a
```

Response:

```json
{
  "job_id": "7c9e6f8d5b2a4e1c9d8f3a6b7e5c2d1a",
  "status": "done",
  "created_at": "2026-04-15T12:30:00",
  "started_at": "2026-04-15T12:30:01",
  "finished_at": "2026-04-15T12:34:56",
  "output_format": "json",
  "page_count": 43,
  "pages_ok": 43,
  "pages_failed": 0,
  "error_message": null,
  "result_url": "/v1/jobs/7c9e6f8d5b2a4e1c9d8f3a6b7e5c2d1a/result"
}
```

### Downloading the result

```bash
curl -H "X-API-Key: sk_live_xxx" \
  http://localhost:8000/v1/jobs/7c9e6f8d5b2a4e1c9d8f3a6b7e5c2d1a/result \
  -o result.json
```

## Output Formats

| Format | MIME | Use case |
|---|---|---|
| `md` | `text/markdown` | human-readable; contains `## Seite N` headings and per-page OCR strategy annotations |
| `txt` | `text/plain` | raw text; pages separated by `\f` (form feed) |
| `toon` | `application/x-toon` | structured legal-document format with `section` detection |
| `json` | `application/json` | full metadata: per-page text, strategy, elapsed time, meta counts |

## Rate Limiting

10 requests per minute per API key by default (configurable via `RATE_LIMIT_PER_MINUTE` in `.env`). Over-limit responses return `429 Too Many Requests` with `Retry-After` and `X-RateLimit-*` headers.

## Deployment Notes

- **Reverse proxy:** put Caddy or Nginx in front of port 8000 for TLS. Example Caddyfile:

  ```caddy
  ocr.example.com {
    reverse_proxy localhost:8000
  }
  ```

  Do NOT expose port 11434 (Ollama) through the proxy.
- **Backups:** the SQLite DB lives at `./data/ocr.db`. Back it up nightly (simple `cp` is fine since SQLite supports hot backups with WAL mode).
- **Cleanup:** result files older than `RESULT_TTL_DAYS` (default 30) can be pruned by a periodic cleanup script. The MVP does not ship one — add a cron job when you wire this up.
- **Scaling:** a single Ollama instance serializes OCR jobs. For real parallelism, run multiple Ollama instances behind a load balancer and point each worker at a different one (requires code changes to `app/services/ocr_pipeline.py`).

## Development

```bash
uv venv --python 3.11 .venv
uv pip install --python .venv/bin/python -e ".[dev]"

.venv/bin/pytest                    # run tests
.venv/bin/ruff check app/           # lint
.venv/bin/ruff format app/          # format

.venv/bin/uvicorn app.main:app --reload --port 8000   # dev server (no docker)
```

For the dev server to work without docker, you still need:

- A running Redis (e.g. `docker run -d -p 6379:6379 redis:7-alpine`)
- A running Ollama with `glm-ocr` pulled
- `.env` configured with:
  - `REDIS_URL=redis://localhost:6379/0`
  - `OLLAMA_URL=http://localhost:11434`
  - `DATABASE_URL=sqlite:///./data/ocr.db`
  - `STORAGE_DIR=./data/storage`

## Architecture

See [`docs/superpowers/specs/2026-04-15-docklyocr-implementation-design.md`](docs/superpowers/specs/2026-04-15-docklyocr-implementation-design.md) for the implementation design and [`OCR-API-Projekt-Anforderungen.md`](OCR-API-Projekt-Anforderungen.md) for the functional specification.

## License

Proprietary — Hylab.
