#!/usr/bin/env bash
# ── DocklyOCR GPU Server Setup ─────────────────────────────────────────
#
# Run this ONCE on a fresh Ubuntu 24.04 "GPU OS" instance, AFTER attaching
# a persistent block-storage volume (min 60 GB) for /var/lib/docker.
#
# Supported instance types:
#   H100-1-80G           single-GPU, TP=1 (default)
#   H100-SXM-2-80G       dual-GPU NVLink, set TP_SIZE=2
#   L40S-1-48G           single-GPU, TP=1
#
# Prerequisites:
#   - SSH as root, persistent 60GB+ volume attached as /dev/sdc
#     (/dev/sdb is the ephemeral Scaleway /scratch — cleared on every stop)
#   - Private NIC attached on the Scaleway side (this script brings it up)
#
# Tunables via env vars (with sensible defaults):
#   NEW_DEV         block-device for persistent docker storage  (default /dev/sdc)
#   TP_SIZE         vLLM --tensor-parallel-size                 (default 1)
#   MODEL_NAME      HF model id                                 (default Qwen/Qwen2.5-VL-7B-Instruct)
#   SERVED_NAME     vLLM --served-model-name                    (default qwen2.5-vl-7b)
#
# Architecture established here:
#   - Docker root on persistent block volume
#   - containerd snapshots bind-mounted onto same volume (avoids root-disk
#     fill-up when vLLM restart-loops)
#   - Netplan brings the private NIC (enp2s0) up via DHCP, persistent
#   - UFW: SSH from anywhere, vLLM port 8000 only from VPC 172.16.8.0/22
#   - Auto-shutdown timer (systemd) explicitly enabled + started
#   - All Ollama leftovers purged (would otherwise eat ~5 GB of root disk)
#
set -euo pipefail

: "${NEW_DEV:=/dev/sdc}"
: "${TP_SIZE:=1}"
: "${MODEL_NAME:=Qwen/Qwen2.5-VL-7B-Instruct}"
: "${SERVED_NAME:=qwen2.5-vl-7b}"

echo "=== Setup parameters ==="
echo "  NEW_DEV     = $NEW_DEV"
echo "  TP_SIZE     = $TP_SIZE"
echo "  MODEL_NAME  = $MODEL_NAME"
echo "  SERVED_NAME = $SERVED_NAME"
echo ""

echo "=== 1/10  System packages ==="
apt-get update
apt-get install -y --no-install-recommends ca-certificates curl jq ufw

echo "=== 2/10  Purge Ollama leftovers (would eat ~5 GB on root disk) ==="
systemctl stop ollama 2>/dev/null || true
systemctl disable ollama 2>/dev/null || true
rm -rf /usr/local/lib/ollama /usr/local/share/ollama /usr/local/bin/ollama*
rm -rf /etc/systemd/system/ollama.service

echo "=== 3/10  Docker (from docker.com repo) ==="
install -m 0755 -d /etc/apt/keyrings
curl -fsSL https://download.docker.com/linux/ubuntu/gpg -o /etc/apt/keyrings/docker.asc
chmod a+r /etc/apt/keyrings/docker.asc
echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.asc] \
https://download.docker.com/linux/ubuntu $(. /etc/os-release && echo "$VERSION_CODENAME") stable" \
  > /etc/apt/sources.list.d/docker.list
apt-get update
DEBIAN_FRONTEND=noninteractive apt-get install -y \
    docker-ce docker-ce-cli containerd.io docker-compose-plugin

echo "=== 4/10  Persistent Docker storage on $NEW_DEV ==="
if ! blkid "$NEW_DEV" > /dev/null 2>&1; then
    echo "Formatting $NEW_DEV (first-time setup) ..."
    mkfs.ext4 -F -L docker-data "$NEW_DEV"
fi
UUID=$(blkid -s UUID -o value "$NEW_DEV")

systemctl stop docker containerd docker.socket 2>/dev/null || true
rm -rf /var/lib/docker
mkdir -p /var/lib/docker
grep -q "$UUID" /etc/fstab || echo "UUID=$UUID /var/lib/docker ext4 defaults,nofail 0 2" >> /etc/fstab
mount /var/lib/docker || true

# Bind-mount /var/lib/containerd to the persistent volume so containerd
# snapshots/temps don't fill the 17 GB root disk during vLLM restart loops.
mkdir -p /var/lib/docker/containerd
rm -rf /var/lib/containerd/* 2>/dev/null || true
mkdir -p /var/lib/containerd
grep -q "/var/lib/docker/containerd /var/lib/containerd" /etc/fstab \
    || echo "/var/lib/docker/containerd /var/lib/containerd none bind 0 0" >> /etc/fstab
mount --bind /var/lib/docker/containerd /var/lib/containerd
# Remove any stale containerd config that pointed at the old _containerd path
rm -f /etc/containerd/config.toml

systemctl enable --now containerd docker
sleep 3
docker info | grep -E "Root Dir|Storage Driver"

echo "=== 5/10  Pull vLLM image ==="
docker pull vllm/vllm-openai:latest
mkdir -p /var/lib/docker/vllm-cache

echo "=== 6/10  Install vLLM systemd service (TP=$TP_SIZE) ==="
cat > /etc/systemd/system/vllm.service << SVC
[Unit]
Description=vLLM OpenAI server ($SERVED_NAME, TP=$TP_SIZE)
After=docker.service network-online.target
Requires=docker.service

[Service]
Type=simple
Restart=always
RestartSec=10
ExecStartPre=-/usr/bin/docker stop vllm
ExecStartPre=-/usr/bin/docker rm vllm
ExecStart=/usr/bin/docker run --rm --name vllm --gpus all --ipc=host \\
  -p 0.0.0.0:8000:8000 \\
  -v /var/lib/docker/vllm-cache:/root/.cache/huggingface \\
  vllm/vllm-openai:latest \\
  --model $MODEL_NAME \\
  --dtype bfloat16 \\
  --max-model-len 32768 \\
  --tensor-parallel-size $TP_SIZE \\
  --gpu-memory-utilization 0.85 \\
  --limit-mm-per-prompt '{"image":1}' \\
  --served-model-name $SERVED_NAME
ExecStop=/usr/bin/docker stop vllm

[Install]
WantedBy=multi-user.target
SVC

systemctl daemon-reload
systemctl enable --now vllm
echo "vLLM starting — model downloads on first boot (~15 GB, ~5 min)."
echo "Subsequent boots reuse the cached weights from the persistent volume."

echo "=== 7/10  Scaleway credentials for auto-shutdown ==="
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

echo "=== 8/10  Install + enable auto-shutdown timer ==="
cp "$(dirname "$0")/auto-shutdown.sh" /usr/local/bin/dockly-auto-shutdown
chmod +x /usr/local/bin/dockly-auto-shutdown
cp "$(dirname "$0")/dockly-auto-shutdown.service" /etc/systemd/system/
cp "$(dirname "$0")/dockly-auto-shutdown.timer" /etc/systemd/system/
systemctl daemon-reload
# `enable --now` is critical — without --now the timer is registered for
# next boot but doesn't fire in the current session, and the install was
# observed silently inactive for weeks.
systemctl enable --now dockly-auto-shutdown.timer
systemctl is-active dockly-auto-shutdown.timer

echo "=== 9/10  Persistent private NIC config (enp2s0) ==="
# The Scaleway "Ubuntu Noble GPU OS" image ships with netplan only for the
# public NIC (enp0s1). The private NIC (enp2s0) stays DOWN until we add it.
cat > /etc/netplan/60-private-net.yaml << 'NPN'
network:
  version: 2
  ethernets:
    enp2s0:
      dhcp4: true
      optional: true
NPN
chmod 600 /etc/netplan/60-private-net.yaml
netplan apply
sleep 2
ip -4 addr show enp2s0 | tail -1 || true

echo "=== 10/10  Firewall ==="
ufw --force reset > /dev/null
ufw default deny incoming
ufw default allow outgoing
ufw allow 22/tcp comment "SSH"
# vLLM reachable ONLY from the Scaleway private network.
# Port 8000 (vLLM) — NOT 11434 (Ollama port — wrong rule was the cause
# of a multi-hour debug session on 2026-05-09).
ufw allow from 172.16.8.0/22 to any port 8000 proto tcp comment "vLLM from PN"
ufw --force enable
ufw status verbose

echo ""
echo "=== DONE ==="
echo "  vLLM service:        systemctl status vllm"
echo "  Model readiness:     curl http://localhost:8000/v1/models"
echo "  Auto-shutdown timer: systemctl list-timers dockly-auto-shutdown.timer"
echo ""
echo "Next: edit /etc/dockly/scw-credentials with real SCW API keys,"
echo "then verify the box is reachable from the API server via the private IP."
