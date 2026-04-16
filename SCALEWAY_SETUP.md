# DocklyOCR auf Scaleway L4-1-24G einrichten

Komplettanleitung: Von frischer GPU-Instanz zu laufendem OCR-Service mit Auto-Shutdown.

## Voraussetzungen

- Scaleway-Account mit aktiviertem GPU-Zugang
- Eine Domain (z. B. `ocr.deine-domain.de`) mit DNS auf die Server-IP
- SSH-Key im Scaleway-Account hinterlegt

## Schritt 1 — GPU-Instanz erstellen

1. Scaleway Console: **Instances > Create Instance**
2. Typ: **L4-1-24G** (NVIDIA L4, 24 GB VRAM)
3. Image: **Ubuntu 22.04 Jammy**
4. Zone: **fr-par-2** (oder die Zone mit GPU-Verfügbarkeit)
5. Storage: **200 GB NVMe** (reicht für Modell + Uploads + Pages)
6. Netzwerk: Public IP aktivieren
7. SSH-Key auswählen

**Notiere dir:**
- Server-ID (z. B. `a1b2c3d4-e5f6-...`) — brauchst du für Auto-Start
- Public IP (z. B. `51.159.xx.xx`)

## Schritt 2 — DNS konfigurieren

Erstelle einen A-Record bei deinem DNS-Provider:

```
ocr.deine-domain.de  →  51.159.xx.xx  (TTL: 300)
```

## Schritt 3 — Per SSH verbinden

```bash
ssh root@51.159.xx.xx
```

## Schritt 4 — System-Pakete installieren

```bash
apt-get update && apt-get upgrade -y
apt-get install -y docker.io docker-compose-plugin caddy curl jq poppler-utils git python3-pip python3-venv
systemctl enable --now docker
```

## Schritt 5 — NVIDIA-Treiber prüfen

Scaleway GPU-Images kommen meist mit vorinstallierten Treibern:

```bash
nvidia-smi
```

Erwartete Ausgabe: NVIDIA L4 mit 24 GB VRAM. Wenn `nvidia-smi` nicht gefunden wird:

```bash
apt-get install -y nvidia-driver-535
reboot
```

## Schritt 6 — Ollama installieren

```bash
curl -fsSL https://ollama.com/install.sh | sh
systemctl enable --now ollama
```

Warte bis Ollama bereit ist:

```bash
until curl -sf http://localhost:11434/api/tags > /dev/null 2>&1; do sleep 2; echo "Waiting..."; done
echo "Ollama ready!"
```

## Schritt 7 — glm-ocr Modell laden

```bash
ollama pull glm-ocr
```

Prüfen:

```bash
ollama list
# Erwartete Ausgabe: glm-ocr:latest  ~2.2 GB
```

## Schritt 8 — DocklyOCR klonen und konfigurieren

```bash
git clone https://github.com/Jfahlbusch/DocklyOCR.git /opt/dockly-ocr
cd /opt/dockly-ocr
cp .env.example .env
```

### .env bearbeiten

```bash
nano .env
```

**Diese Werte MÜSSEN gesetzt werden:**

```env
# Ollama lokal (NICHT host.docker.internal — hier läuft alles auf einer Box)
OLLAMA_URL=http://172.17.0.1:11434
OLLAMA_REQUEST_TIMEOUT_S=30

# Admin-Passwort generieren:
#   python3 -c "import bcrypt; print(bcrypt.hashpw(b'DEIN-PASSWORT', bcrypt.gensalt()).decode())"
# Achtung: $ in der Ausgabe mit $$ escapen für docker-compose!
ADMIN_PASSWORD_HASH=$$2b$$12$$...

# Session-Secret generieren:
#   python3 -c "import secrets; print(secrets.token_urlsafe(48))"
SESSION_SECRET=dein-generiertes-secret

# Scaleway API Keys (für Auto-Start wenn GPU aus ist)
SCW_ACCESS_KEY=SCWxxxxxxxxxxxxxxxxx
SCW_SECRET_KEY=xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx
SCW_GPU_SERVER_ID=xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx
SCW_GPU_ZONE=fr-par-2
```

> **Wichtig:** `OLLAMA_URL` ist `http://172.17.0.1:11434` (Docker-Bridge-Gateway), nicht `localhost`. Die Container erreichen den Host-Ollama uber die Docker-Bridge-IP.

### Passwort und Secret generieren

```bash
# Python venv fur Scripts
python3 -m venv /opt/dockly-ocr/.venv-host
/opt/dockly-ocr/.venv-host/bin/pip install -q -e "/opt/dockly-ocr"

# Admin-Passwort (Ausgabe in .env eintragen, $ mit $$ escapen!)
/opt/dockly-ocr/.venv-host/bin/python scripts/hash_password.py 'dein-starkes-passwort'

# Session-Secret
python3 -c "import secrets; print(secrets.token_urlsafe(48))"
```

## Schritt 9 — Datenbank initialisieren

```bash
cd /opt/dockly-ocr
.venv-host/bin/python scripts/init_db.py
```

Erwartete Ausgabe:

```
DocklyOCR DB init — database_url=sqlite:////data/ocr.db
  - tables created (or already existed)
  - admin user 'admin' created
Done.
```

## Schritt 10 — Docker-Stack starten

```bash
cd /opt/dockly-ocr
docker compose up -d --build
```

Prüfen:

```bash
docker compose ps
# api: healthy, redis: healthy, worker: running

curl http://localhost:8000/health
# {"status":"ok","ollama":"ok","db":"ok"}
```

Wenn `ollama: unreachable` → prüfe ob `172.17.0.1:11434` vom Container erreichbar ist:

```bash
docker exec docklyocr-api-1 curl -s http://172.17.0.1:11434/api/tags
```

Falls nicht: `extra_hosts` in `docker-compose.yml` anpassen oder Ollama auf `0.0.0.0` binden:

```bash
# /etc/systemd/system/ollama.service.d/override.conf
[Service]
Environment="OLLAMA_HOST=0.0.0.0"
```

```bash
systemctl daemon-reload && systemctl restart ollama
```

## Schritt 11 — Caddy (HTTPS) einrichten

```bash
nano /etc/caddy/Caddyfile
```

Inhalt:

```caddy
ocr.deine-domain.de {
    reverse_proxy localhost:8000
}
```

```bash
systemctl enable --now caddy
```

Caddy holt automatisch ein Let's-Encrypt-Zertifikat. Nach ~30 Sekunden:

```bash
curl https://ocr.deine-domain.de/health
# {"status":"ok","ollama":"ok","db":"ok"}
```

## Schritt 12 — Auto-Shutdown einrichten

```bash
cp scripts/scaleway-deploy/auto-shutdown.sh /usr/local/bin/dockly-auto-shutdown
chmod +x /usr/local/bin/dockly-auto-shutdown
cp scripts/scaleway-deploy/dockly-auto-shutdown.service /etc/systemd/system/
cp scripts/scaleway-deploy/dockly-auto-shutdown.timer /etc/systemd/system/
systemctl daemon-reload
systemctl enable --now dockly-auto-shutdown.timer
```

Prüfen:

```bash
systemctl status dockly-auto-shutdown.timer
# Active: active (waiting)
```

**Verhalten:** Jede Minute wird geprüft ob Jobs laufen. Nach 2 Minuten ohne Jobs fährt der Server automatisch runter.

## Schritt 13 — Cleanup-Cronjob einrichten

```bash
CRON_LINE="0 3 * * * cd /opt/dockly-ocr && .venv-host/bin/python scripts/cleanup_old_results.py --delete --days 7 >> /var/log/dockly-cleanup.log 2>&1"
(crontab -l 2>/dev/null | grep -v "cleanup_old_results" ; echo "$CRON_LINE") | crontab -
```

Prüfen:

```bash
crontab -l
# 0 3 * * * cd /opt/dockly-ocr && ... cleanup_old_results.py --delete --days 7
```

Löscht täglich um 03:00 Uhr alle Jobs älter als 7 Tage (Uploads, Seitenbilder, Ergebnisse + DB-Einträge).

## Schritt 14 — Ersten OCR-Test durchführen

### Admin-UI

1. Öffne `https://ocr.deine-domain.de/admin`
2. Login mit `admin` / dein Passwort
3. Neuen Kunden anlegen
4. API-Key generieren (Klartext kopieren!)

### API-Test

```bash
KEY="sk_live_xxxxxxxxxxxxxxxxxxxxxxxxxx"

# Sync (kleine Datei)
curl -X POST https://ocr.deine-domain.de/v1/ocr \
  -H "X-API-Key: $KEY" \
  -F "file=@test.pdf" \
  -F "output_format=md" \
  -F "mode=sync" \
  -o result.md

# Async (große Datei)
curl -X POST https://ocr.deine-domain.de/v1/ocr \
  -H "X-API-Key: $KEY" \
  -F "file=@grosses-dokument.pdf" \
  -F "output_format=md" \
  -F "mode=async"
# → {"job_id": "xxx", "status": "pending", "status_url": "/v1/jobs/xxx"}

# Status prüfen
curl -H "X-API-Key: $KEY" https://ocr.deine-domain.de/v1/jobs/xxx

# Ergebnis herunterladen
curl -H "X-API-Key: $KEY" https://ocr.deine-domain.de/v1/jobs/xxx/result -o result.md
```

## Kosten-Übersicht

| Posten | Kosten |
|---|---|
| L4-1-24G pro Stunde | ~€1.12/h |
| Auto-Shutdown Idle | 2 Min (~€0.04 pro Zyklus) |
| Beispiel: 5 Jobs/Tag, je 2 Min | ~€0.30/Tag |
| Beispiel: 20 Jobs/Tag, je 2 Min | ~€1.20/Tag |
| Storage (200 GB NVMe) | ~€15/Mo |

## Troubleshooting

### `ollama: unreachable` im Health-Check

```bash
# Ollama läuft?
systemctl status ollama

# Vom Container erreichbar?
docker exec docklyocr-api-1 curl -s http://172.17.0.1:11434/api/tags

# Falls nicht: Ollama auf alle Interfaces binden
systemctl edit ollama
# [Service]
# Environment="OLLAMA_HOST=0.0.0.0"
systemctl restart ollama
```

### GPU wird nicht erkannt

```bash
nvidia-smi  # Treiber OK?
ollama list  # Modell geladen?
```

### Auto-Shutdown greift nicht

```bash
systemctl status dockly-auto-shutdown.timer
journalctl -u dockly-auto-shutdown -f
```

### Cleanup prüfen

```bash
cd /opt/dockly-ocr
.venv-host/bin/python scripts/cleanup_old_results.py  # Dry-run
cat /var/log/dockly-cleanup.log  # Letzte Ausführung
```

### Server manuell starten (nach Auto-Shutdown)

Per Scaleway Console: Instanz starten. Oder per API:

```bash
python3 scripts/scaleway-deploy/start-gpu.py
```
