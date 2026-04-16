#!/usr/bin/env bash
# ── DocklyOCR GPU Server Setup (Scaleway L4-1-24G) ──────────────────
#
# Run this ONCE on a fresh Ubuntu 22.04 GPU instance.
# Prerequisites: SSH access, root or sudo.
#
# Architecture:
#   - Ollama runs natively (systemd, GPU access, bound to 127.0.0.1)
#   - DocklyOCR runs in Docker (api + worker + redis)
#   - Caddy handles HTTPS
#   - Auto-shutdown after idle (no jobs for 15 min)
#
set -euo pipefail

echo "=== 1/7 System packages ==="
apt-get update
apt-get install -y --no-install-recommends \
    docker.io docker-compose-plugin \
    caddy \
    poppler-utils \
    curl \
    jq

systemctl enable --now docker

echo "=== 2/7 Install Ollama ==="
curl -fsSL https://ollama.com/install.sh | sh
systemctl enable --now ollama

# Wait for Ollama to be ready
echo "Waiting for Ollama..."
until curl -sf http://localhost:11434/api/tags > /dev/null 2>&1; do sleep 2; done

echo "=== 3/7 Pull glm-ocr model ==="
ollama pull glm-ocr

echo "=== 4/7 Clone and configure DocklyOCR ==="
if [ ! -d /opt/dockly-ocr ]; then
    echo "IMPORTANT: Clone the repo to /opt/dockly-ocr first!"
    echo "  git clone <your-repo-url> /opt/dockly-ocr"
    echo "Then re-run this script."
    exit 1
fi

cd /opt/dockly-ocr

if [ ! -f .env ]; then
    cp .env.example .env
    echo ""
    echo "IMPORTANT: Edit /opt/dockly-ocr/.env — set these values:"
    echo "  OLLAMA_URL=http://localhost:11434"
    echo "  ADMIN_PASSWORD_HASH=<bcrypt hash>"
    echo "  SESSION_SECRET=<random 48+ chars>"
    echo ""
    echo "Generate with:"
    echo "  python3 scripts/hash_password.py 'your-password'"
    echo "  python3 -c \"import secrets; print(secrets.token_urlsafe(48))\""
    exit 1
fi

echo "=== 5/7 Initialize database ==="
# Install Python deps for init script (host-side, one-time)
apt-get install -y python3-pip python3-venv
python3 -m venv /opt/dockly-ocr/.venv-host
/opt/dockly-ocr/.venv-host/bin/pip install -q -e "/opt/dockly-ocr[dev]"
/opt/dockly-ocr/.venv-host/bin/python scripts/init_db.py

echo "=== 6/7 Start Docker stack ==="
docker compose up -d --build

echo "=== 7/7 Setup auto-shutdown ==="
# Install the idle-shutdown timer (see auto-shutdown.sh)
cp scripts/scaleway-deploy/auto-shutdown.sh /usr/local/bin/dockly-auto-shutdown
chmod +x /usr/local/bin/dockly-auto-shutdown
cp scripts/scaleway-deploy/dockly-auto-shutdown.service /etc/systemd/system/
cp scripts/scaleway-deploy/dockly-auto-shutdown.timer /etc/systemd/system/
systemctl daemon-reload
systemctl enable --now dockly-auto-shutdown.timer

echo "=== 8/8 Setup daily cleanup cron (delete jobs older than 7 days) ==="
CRON_LINE="0 3 * * * cd /opt/dockly-ocr && .venv-host/bin/python scripts/cleanup_old_results.py --delete --days 7 >> /var/log/dockly-cleanup.log 2>&1"
(crontab -l 2>/dev/null | grep -v "cleanup_old_results" ; echo "$CRON_LINE") | crontab -

echo ""
echo "=== DONE ==="
echo "API:    http://$(hostname -I | awk '{print $1}'):8000"
echo "Admin:  http://$(hostname -I | awk '{print $1}'):8000/admin"
echo "Health: curl http://localhost:8000/health"
echo ""
echo "Auto-shutdown: GPU will power off after 15 min idle (no processing jobs)."
echo "To disable: systemctl disable dockly-auto-shutdown.timer"
