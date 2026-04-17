#!/usr/bin/env bash
# ── DocklyOCR GPU Server Setup (Scaleway H100-1-80G / L4-1-24G) ─────────
#
# Run this ONCE on a fresh Ubuntu 24.04 "GPU OS" instance, AFTER attaching
# a persistent block-storage volume (min 60 GB) for /var/lib/docker.
#
# Prerequisites: SSH as root, a persistent 60GB+ volume attached as /dev/sdb
# (Scaleway's /scratch is EPHEMERAL — cleared on every stop/start).
#
# Architecture set up here:
#   - Docker with root on persistent volume (/dev/sdb → /var/lib/docker)
#   - vLLM Docker container running Qwen2.5-VL-7B-Instruct (bfloat16)
#   - Model cache on the same persistent volume so cold-starts are fast
#   - Auto-shutdown timer (systemd) that calls Scaleway API to poweroff
#     the instance when idle — no billing when no jobs are running
#
set -euo pipefail

echo "=== 1/8  System packages ==="
apt-get update
apt-get install -y --no-install-recommends \
    ca-certificates curl jq

echo "=== 2/8  Docker (from docker.com repo) ==="
install -m 0755 -d /etc/apt/keyrings
curl -fsSL https://download.docker.com/linux/ubuntu/gpg -o /etc/apt/keyrings/docker.asc
chmod a+r /etc/apt/keyrings/docker.asc
echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.asc] \
https://download.docker.com/linux/ubuntu $(. /etc/os-release && echo "$VERSION_CODENAME") stable" \
  > /etc/apt/sources.list.d/docker.list
apt-get update
DEBIAN_FRONTEND=noninteractive apt-get install -y \
    docker-ce docker-ce-cli containerd.io docker-compose-plugin

echo "=== 3/8  Persistent Docker storage on /dev/sdb ==="
NEW_DEV=/dev/sdb
if ! blkid "$NEW_DEV" > /dev/null 2>&1; then
    echo "Formatting $NEW_DEV (first-time setup) ..."
    mkfs.ext4 -F -L docker-data "$NEW_DEV"
fi
UUID=$(blkid -s UUID -o value "$NEW_DEV")

# Mount persistently across reboots
rm -rf /var/lib/docker
mkdir -p /var/lib/docker
grep -q "$UUID" /etc/fstab || echo "UUID=$UUID /var/lib/docker ext4 defaults,nofail 0 2" >> /etc/fstab
mount /var/lib/docker || true

# containerd stores image ingests somewhere, relocate to persistent volume
mkdir -p /var/lib/docker/_containerd
cat > /etc/containerd/config.toml << EOF
version = 2
root = "/var/lib/docker/_containerd"
state = "/run/containerd"
EOF

systemctl enable --now containerd docker
sleep 3
docker info | grep "Root Dir"

echo "=== 4/8  Pull vLLM image ==="
docker pull vllm/vllm-openai:latest
mkdir -p /var/lib/docker/vllm-cache

echo "=== 5/8  Install vLLM systemd service ==="
cat > /etc/systemd/system/vllm.service << 'SVC'
[Unit]
Description=vLLM OpenAI-compatible server (Qwen2.5-VL-7B)
After=docker.service
Requires=docker.service

[Service]
Type=simple
Restart=always
RestartSec=10
ExecStartPre=-/usr/bin/docker stop vllm
ExecStartPre=-/usr/bin/docker rm vllm
ExecStart=/usr/bin/docker run --rm --name vllm --gpus all --ipc=host \
  -p 0.0.0.0:8000:8000 \
  -v /var/lib/docker/vllm-cache:/root/.cache/huggingface \
  vllm/vllm-openai:latest \
  --model Qwen/Qwen2.5-VL-7B-Instruct \
  --dtype bfloat16 \
  --max-model-len 32768 \
  --gpu-memory-utilization 0.85 \
  --limit-mm-per-prompt '{"image":1}' \
  --served-model-name qwen2.5-vl-7b
ExecStop=/usr/bin/docker stop vllm

[Install]
WantedBy=multi-user.target
SVC

systemctl daemon-reload
systemctl enable --now vllm
echo "vLLM starting — model will download on first boot (~15 GB, ~5 min)."
echo "Subsequent boots reuse the cached weights from the persistent volume."

echo "=== 6/8  Scaleway credentials for auto-shutdown ==="
if [ ! -f /etc/dockly/scw-credentials ]; then
    mkdir -p /etc/dockly
    cat > /etc/dockly/scw-credentials << EOF
SCW_ACCESS_KEY=REPLACE_ME
SCW_SECRET_KEY=REPLACE_ME
SCW_GPU_SERVER_ID=REPLACE_ME
SCW_GPU_ZONE=fr-par-2
EOF
    chmod 600 /etc/dockly/scw-credentials
    echo ""
    echo "*** EDIT /etc/dockly/scw-credentials with your Scaleway API keys. ***"
    echo ""
fi

echo "=== 7/8  Install auto-shutdown timer ==="
cp "$(dirname "$0")/auto-shutdown.sh" /usr/local/bin/dockly-auto-shutdown
chmod +x /usr/local/bin/dockly-auto-shutdown
cp "$(dirname "$0")/dockly-auto-shutdown.service" /etc/systemd/system/
cp "$(dirname "$0")/dockly-auto-shutdown.timer" /etc/systemd/system/
systemctl daemon-reload
systemctl enable --now dockly-auto-shutdown.timer

echo "=== 8/8  Firewall ==="
if command -v ufw > /dev/null; then
    ufw --force reset
    ufw default deny incoming
    ufw default allow outgoing
    ufw allow 22/tcp comment "SSH"
    # vLLM port reachable ONLY from the Scaleway private network
    ufw allow from 172.16.8.0/22 to any port 8000 proto tcp comment "vLLM from PN"
    ufw --force enable
    ufw status verbose
fi

echo ""
echo "=== DONE ==="
echo "  vLLM service:  systemctl status vllm"
echo "  Model status:  curl http://localhost:8000/v1/models"
echo "  Auto-shutdown: GPU powers off after the idle threshold (120s idle)"
echo "                 via Scaleway API, so no GPU billing when idle."
echo ""
echo "Next: edit /etc/dockly/scw-credentials with real SCW API keys,"
echo "then test end-to-end by uploading a document via the API server."
