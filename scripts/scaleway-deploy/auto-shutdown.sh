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
IDLE_THRESHOLD_SECONDS=120  # shut down after 2 min idle — H100 is expensive

VLLM_URL="http://localhost:8000"

# Reset idle timer if vLLM is busy OR still warming up.
#
# "Busy" = vLLM reports any running/waiting requests in its Prometheus
# metrics. "Warming up" = the container is started but /v1/models is
# not yet 200 (CUDA graph compile on cold start takes ~3 min and must
# not be killed, or the worker's ensure_gpu_running() polls forever).
ACTIVE=0

# 1) Loading phase — container running but API not ready yet
if systemctl is-active --quiet vllm; then
    if ! curl -sf --max-time 2 "${VLLM_URL}/v1/models" > /dev/null 2>&1; then
        ACTIVE=1
    fi
fi

# 2) Serving phase — any in-flight or queued requests
if [ "$ACTIVE" = "0" ]; then
    RUNNING=$(curl -sf --max-time 2 "${VLLM_URL}/metrics" 2>/dev/null \
        | awk '/^vllm:num_requests_(running|waiting)\{/ {sum += $2} END {print sum+0}')
    if [ -n "${RUNNING:-}" ] && [ "${RUNNING%.*}" -gt 0 ] 2>/dev/null; then
        ACTIVE=1
    fi
fi

NOW=$(date +%s)

# Active → reset idle timer, keep GPU running
if [ "$ACTIVE" = "1" ]; then
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
