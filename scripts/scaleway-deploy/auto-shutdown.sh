#!/usr/bin/env bash
# ── DocklyOCR Auto-Shutdown ──────────────────────────────────────────
#
# Called every 5 minutes by systemd timer.
# If no OCR jobs were processed in the last 15 minutes → power off.
#
# This saves costs: the Scaleway L4-1-24G GPU only runs while needed.
# The API server (on a separate cheap CPU instance, or the same box)
# is responsible for booting the GPU via Scaleway API when a new job
# arrives.
#
set -euo pipefail

IDLE_FILE="/tmp/dockly-last-activity"
IDLE_THRESHOLD_SECONDS=900  # 15 minutes

# Check if any jobs are currently processing
PROCESSING=$(curl -sf http://localhost:8000/v1/jobs?status=processing 2>/dev/null \
    | python3 -c "import json,sys; d=json.load(sys.stdin); print(d.get('total',0))" 2>/dev/null \
    || echo "0")

if [ "$PROCESSING" -gt 0 ]; then
    # Active jobs → update activity timestamp
    date +%s > "$IDLE_FILE"
    exit 0
fi

# Check worker queue (Redis)
QUEUED=$(docker exec docklyocr-redis-1 redis-cli LLEN arq:queue 2>/dev/null || echo "0")
if [ "$QUEUED" -gt 0 ]; then
    date +%s > "$IDLE_FILE"
    exit 0
fi

# No active or queued jobs — check idle duration
if [ ! -f "$IDLE_FILE" ]; then
    # First check, no activity file → start tracking
    date +%s > "$IDLE_FILE"
    exit 0
fi

LAST_ACTIVITY=$(cat "$IDLE_FILE")
NOW=$(date +%s)
IDLE_SECONDS=$((NOW - LAST_ACTIVITY))

if [ "$IDLE_SECONDS" -ge "$IDLE_THRESHOLD_SECONDS" ]; then
    echo "$(date): DocklyOCR idle for ${IDLE_SECONDS}s (threshold: ${IDLE_THRESHOLD_SECONDS}s). Shutting down."
    logger -t dockly-auto-shutdown "Idle for ${IDLE_SECONDS}s, powering off GPU instance."

    # Graceful shutdown: stop Docker first, then power off
    docker compose -f /opt/dockly-ocr/docker-compose.yml down --timeout 10 2>/dev/null || true
    /sbin/poweroff
fi
