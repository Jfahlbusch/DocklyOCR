# DocklyOCR on Scaleway — Production Deployment

Full walk-through from empty Scaleway project to a running OCR service with on-demand GPU, HTTPS, persistent model cache, and auto-shutdown.

## Target Architecture

```
  Internet ──▶ Caddy (HTTPS) ──▶ DEV1-M (API + worker + Redis + SQLite)
                                         │
                                         │  private network (172.16.8.0/22)
                                         ▼
                                  H100-1-80G (on-demand)
                                  └── vLLM + Qwen2.5-VL-7B
                                      persistent block volume for image + model cache
                                      auto-stop after idle → zero GPU billing
```

- **Always-on CPU instance** (`DEV1-M`, ~€9/month): API, worker, admin UI
- **On-demand GPU instance** (`H100-1-80G`, €2.73/h *only while running*): vLLM + Qwen
- **Persistent block volume** (~€3/month): keeps the 15 GB Qwen model and vLLM image across GPU shutdowns — cold-starts take ~3 minutes instead of re-downloading everything

## Prerequisites

- Scaleway account with GPU quota in `fr-par-2` (H100 availability varies)
- A domain with DNS pointing at your CPU instance's public IP
- SSH key registered in Scaleway
- Scaleway API credentials (Access Key + Secret Key) with Instance write permissions

## Step 1 — Create the instances

```bash
# CPU instance (API server, always on)
scw instance server create \
  name=DocklyOCR-API \
  type=DEV1-M \
  zone=fr-par-1 \
  project-id=<YOUR_PROJECT> \
  image=ubuntu_noble \
  ip=new

# GPU instance (will be powered off when idle)
scw instance server create \
  name=DocklyOCR \
  type=H100-1-80G \
  zone=fr-par-2 \
  project-id=<YOUR_PROJECT> \
  image=ubuntu_noble_gpu_os_13_nvidia \
  ip=new
```

Record the **server IDs** and the GPU's IP — you'll need them.

## Step 2 — Attach a persistent block volume to the GPU

```bash
scw block volume create \
  name=DocklyOCR-ModelCache \
  zone=fr-par-2 \
  project-id=<YOUR_PROJECT> \
  perf-iops=5000 \
  from-empty.size=60GB

# Wait for status=available, then:
scw instance server attach-volume \
  server-id=<GPU_SERVER_ID> \
  volume-id=<VOLUME_ID> \
  volume-type=sbs_volume \
  zone=fr-par-2
```

The volume appears inside the GPU instance as `/dev/sdb`.

## Step 3 — Put both instances on the same private network

In the Scaleway console, create (or reuse) a **VPC private network** in the `fr-par` region with subnet `172.16.8.0/22`. Attach both instances.

Record the private IPs (e.g., API `172.16.8.15`, GPU `172.16.8.3`).

## Step 4 — Configure DNS

Point an `A` record (e.g., `ocr.example.com`) at the **API server's public IP**.

## Step 5 — Set up the GPU instance

```bash
ssh root@<GPU_PUBLIC_IP>

# Clone repo (just for the setup script)
apt-get update && apt-get install -y git
git clone https://github.com/Jfahlbusch/DocklyOCR.git /opt/dockly-ocr
cd /opt/dockly-ocr

# Run the GPU setup script (installs Docker, vLLM, auto-shutdown)
bash scripts/scaleway-deploy/setup-gpu.sh
```

The script:
1. Installs Docker from the official repo
2. Formats `/dev/sdb` and mounts it at `/var/lib/docker` (persistent model cache)
3. Pulls the `vllm/vllm-openai` image and sets up the systemd service
4. Installs the auto-shutdown timer

**Fill in `/etc/dockly/scw-credentials`** with your Scaleway API keys — required for the auto-shutdown to call `poweroff` via the Scaleway API:

```ini
SCW_ACCESS_KEY=SCW...
SCW_SECRET_KEY=<uuid>
SCW_GPU_SERVER_ID=<GPU_SERVER_ID>
SCW_GPU_ZONE=fr-par-2
```

Verify vLLM came up (first boot downloads the 15 GB model — ~5 min):

```bash
systemctl status vllm
curl http://localhost:8000/v1/models   # returns the served model list once ready
```

## Step 6 — Set up the API instance

```bash
ssh root@<API_PUBLIC_IP>

apt-get update
apt-get install -y ca-certificates curl gnupg git caddy python3-pip python3-venv

# Docker via docker.com repo
install -m 0755 -d /etc/apt/keyrings
curl -fsSL https://download.docker.com/linux/ubuntu/gpg -o /etc/apt/keyrings/docker.asc
chmod a+r /etc/apt/keyrings/docker.asc
echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.asc] https://download.docker.com/linux/ubuntu $(. /etc/os-release && echo "$VERSION_CODENAME") stable" \
  > /etc/apt/sources.list.d/docker.list
apt-get update
DEBIAN_FRONTEND=noninteractive apt-get install -y \
  docker-ce docker-ce-cli containerd.io docker-compose-plugin
systemctl enable --now docker

# Clone the repo
git clone https://github.com/Jfahlbusch/DocklyOCR.git /opt/dockly-ocr
cd /opt/dockly-ocr

# Host-side venv (for scripts like hash_password.py, init_db.py)
python3 -m venv .venv-host
.venv-host/bin/pip install -e .
```

### Configure `.env`

```bash
cp .env.example .env
```

Edit:

```ini
# Backend points at the GPU via the private network (port 8000 on the GPU's private IP)
BACKEND_URL=http://172.16.8.3:8000
BACKEND_MODEL=qwen2.5-vl-7b
BACKEND_REQUEST_TIMEOUT_S=120

# Admin credentials — escape every $ as $$ for docker-compose
ADMIN_PASSWORD_HASH=$$2b$$12$$...
SESSION_SECRET=<random 48 chars>

# On-demand GPU
SCW_ACCESS_KEY=...
SCW_SECRET_KEY=...
SCW_GPU_SERVER_ID=<GPU_SERVER_ID>
SCW_GPU_ZONE=fr-par-2
```

Generate hashes/secrets:

```bash
.venv-host/bin/python scripts/hash_password.py 'your-strong-password'
python3 -c "import secrets; print(secrets.token_urlsafe(48))"
```

### Initialize DB and start the stack

```bash
docker compose up -d --build
docker compose exec api python scripts/init_db.py
curl http://localhost:8000/health
```

### Caddy for HTTPS

```bash
cat > /etc/caddy/Caddyfile << 'EOF'
ocr.example.com {
    reverse_proxy localhost:8000
}
EOF
systemctl enable --now caddy
```

Caddy fetches a Let's Encrypt certificate automatically. After ~30 s:

```bash
curl https://ocr.example.com/health
```

### Firewall (API server)

```bash
ufw --force reset
ufw default deny incoming
ufw default allow outgoing
ufw allow 22/tcp
ufw allow 80/tcp
ufw allow 443/tcp
ufw allow from 172.16.8.0/22 comment "private network"
ufw --force enable
```

Port 8000 is **only** bound to `127.0.0.1` in `docker-compose.yml`, so only Caddy can reach it.

### Daily cleanup cron

```bash
CRON_LINE="0 3 * * * cd /opt/dockly-ocr && .venv-host/bin/python scripts/cleanup_old_results.py --delete --days 7 >> /var/log/dockly-cleanup.log 2>&1"
(crontab -l 2>/dev/null | grep -v cleanup_old_results; echo "$CRON_LINE") | crontab -
```

## Step 7 — First use

1. `https://ocr.example.com/admin` — log in with the credentials from `.env`
2. Create a customer, generate an API key (plaintext shown **once**)
3. Upload a document via **Scalar** (`/scalar`) — supports multi-file batches out of the box
4. First upload triggers a cold start: the GPU boots, vLLM loads the model, OCR runs, GPU auto-stops ~2 minutes after the last job

## Cost Model

| Resource | Cost | Notes |
|---|---|---|
| `DEV1-M` (API) | ~€9/month | always on |
| `H100-1-80G` (GPU) | €2.73/h | **only while running** |
| Block volume (60 GB) | ~€3/month | persistent model cache |
| Scaleway IPs + traffic | ~€1/month | |

Example: 10 jobs/day × ~4 min/job (cold-start) + ~2 min idle each → ~1 GPU-hour/day ≈ **€80/month GPU + €13/month infra**.

## Troubleshooting

**`{"backend":"unreachable"}` on `/health`**
- GPU is probably powered off. Worker will auto-start it on the next job.
- Check private-network connectivity: `ssh root@<API> curl -v http://172.16.8.3:8000/v1/models`

**Cold-start fails with `GPU boot timeout`**
- Usually means the auto-shutdown timer on the GPU fires before vLLM finishes loading.
- Check: `ssh root@<GPU> journalctl -u dockly-auto-shutdown` — the script must detect the vLLM-loading state as "active" to reset the idle timer.

**All pages return OCR-Fehler despite GPU being up**
- vLLM's `/v1/models` answers 200 before inference is ready (CUDA graph compile).
- Mitigation: `ensure_gpu_running()` currently waits up to 10 min for the first successful `/v1/models` — extend with a real inference smoke-test if needed.

**Rebuild the API after pulling new code:**
```bash
cd /opt/dockly-ocr && git pull && docker compose up -d --build
```

**Manually poweroff the GPU:**
```bash
scw instance server stop server-id=<GPU_SERVER_ID> zone=fr-par-2
```
