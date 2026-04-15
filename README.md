# DocklyOCR

Self-hosted OCR API based on `glm-ocr` (Ollama) with a 13-strategy multi-fallback pipeline, API-key authentication, webhook delivery, and a minimal admin UI.

**Status:** in development — see `docs/superpowers/specs/2026-04-15-docklyocr-implementation-design.md` for the current implementation plan.

## Features

- PDF and image input (JPG, PNG, TIFF) up to 100 MB
- 13-strategy fallback pipeline — 100% success rate on legal documents
- Output formats: Markdown, plain text, TOON, JSON
- Synchronous or asynchronous (webhook) delivery
- API-key authentication with prefix-visible, hash-stored keys
- Admin UI for managing customers and keys
- Self-hostable via `docker compose up`
- Swagger / OpenAPI docs at `/docs`

## Prerequisites

- Docker + docker-compose
- Ollama running on the **host** (not in Docker), bound to `127.0.0.1:11434`
- `glm-ocr` model pulled: `ollama pull glm-ocr`

> **Important:** Ollama is never exposed externally — only the DocklyOCR API (port 8000) is reachable from clients. Ollama serves requests exclusively over loopback to the DocklyOCR API container, which talks to it via `host.docker.internal:11434`.

## Quickstart

```bash
# 1. Configure
cp .env.example .env
python scripts/hash_password.py 'choose-an-admin-password'    # paste into ADMIN_PASSWORD_HASH
python -c "import secrets; print(secrets.token_urlsafe(48))"  # paste into SESSION_SECRET

# 2. Initialize database
python scripts/init_db.py

# 3. Start the stack
docker compose up -d --build

# 4. Verify
curl http://localhost:8000/health
```

- **API:** http://localhost:8000
- **Admin UI:** http://localhost:8000/admin
- **Swagger:** http://localhost:8000/docs

## First Steps

1. Open http://localhost:8000/admin and log in with the credentials from `.env`
2. Create a customer
3. Generate an API key (copy the plaintext — it's shown only once)
4. Test the API:

```bash
curl -X POST http://localhost:8000/v1/ocr \
  -H "X-API-Key: sk_live_xxx" \
  -F "file=@document.pdf" \
  -F "output_format=md" \
  -F "mode=sync" \
  -o result.md
```

## Architecture

See [`docs/superpowers/specs/2026-04-15-docklyocr-implementation-design.md`](docs/superpowers/specs/2026-04-15-docklyocr-implementation-design.md) for the implementation design and [`OCR-API-Projekt-Anforderungen.md`](OCR-API-Projekt-Anforderungen.md) for the functional specification.

## License

Proprietary — Hylab.
