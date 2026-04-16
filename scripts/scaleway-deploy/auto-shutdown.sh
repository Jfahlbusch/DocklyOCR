#!/usr/bin/env bash
# ── DocklyOCR GPU Auto-Shutdown (via Scaleway API) ─────────────────
#
# Runs every minute. Stops the VM via Scaleway API after 2 min idle.
# Using Scaleway API (not just /sbin/poweroff) guarantees Scaleway
# sees the server as "stopped" → NO GPU billing.
#
# Requires /etc/dockly/scw-credentials (mode 600) with:
#   SCW_ACCESS_KEY, SCW_SECRET_KEY, SCW_GPU_SERVER_ID, SCW_GPU_ZONE
#
set -euo pipefail
source /etc/dockly/scw-credentials

IDLE_FILE="/tmp/dockly-last-activity"
IDLE_THRESHOLD_SECONDS=120

# Check if Ollama has a model loaded (= recent/current inference)
MODEL_LOADED=$(curl -sf http://localhost:11434/api/ps 2>/dev/null \
    | python3 -c "
import json, sys
try:
    d = json.load(sys.stdin)
    print('1' if d.get('models', []) else '0')
except:
    print('0')
" 2>/dev/null || echo "0")

NOW=$(date +%s)

# Active model → reset idle timer, keep GPU running
if [ "$MODEL_LOADED" = "1" ]; then
    date +%s > "$IDLE_FILE"
    exit 0
fi

# First check (no activity file yet) → initialize timer
if [ ! -f "$IDLE_FILE" ]; then
    date +%s > "$IDLE_FILE"
    exit 0
fi

LAST_ACTIVITY=$(cat "$IDLE_FILE")
IDLE_SECONDS=$((NOW - LAST_ACTIVITY))

if [ "$IDLE_SECONDS" -ge "$IDLE_THRESHOLD_SECONDS" ]; then
    logger -t dockly-auto-shutdown "Idle ${IDLE_SECONDS}s, stopping via Scaleway API."

    # Call Scaleway API — server state transitions to "stopped", no GPU billing
    curl -sSf -X POST \
        "https://api.scaleway.com/instance/v1/zones/${SCW_GPU_ZONE}/servers/${SCW_GPU_SERVER_ID}/action" \
        -H "X-Auth-Token: ${SCW_SECRET_KEY}" \
        -H "Content-Type: application/json" \
        -d '{"action": "poweroff"}' > /dev/null 2>&1 \
        || logger -t dockly-auto-shutdown "Scaleway API call failed, falling back to local poweroff"

    # Wait a moment, then ensure local shutdown as backup
    sleep 15
    /sbin/poweroff
fi
