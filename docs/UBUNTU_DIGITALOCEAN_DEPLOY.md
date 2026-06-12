# Ubuntu DigitalOcean Droplet Deployment

Target: Ubuntu 22.04 or 24.04 LTS on a DigitalOcean droplet.

## 1. Copy Repository

```bash
sudo mkdir -p /opt/horizon-lab
sudo chown "$USER":"$USER" /opt/horizon-lab
git clone <your-github-repo-url> /opt/horizon-lab
cd /opt/horizon-lab
```

Before GitHub check-in, you can upload the folder with `scp` or the DigitalOcean console.

## 2. Prepare Private Env File

Do not commit real secrets to GitHub. Keep environment-specific secrets on the droplet or in your secret manager.

Create a private env file on the droplet:

```bash
sudo cp .env.production.example /root/horizon-prod.env
sudo nano /root/horizon-prod.env
sudo chmod 600 /root/horizon-prod.env
```

Set strong values:

```text
MYSQL_PASSWORD=...
MYSQL_ROOT_PASSWORD=...
testnet_key=...
testnet_secret=...
```

Keep `SYSTEM_STAGE=training` until production validation passes.

## 3. Check Droplet Readiness

Run the installer in check-only mode before installing:

```bash
sudo bash scripts/install_ubuntu.sh --check --env-file /root/horizon-prod.env
```

The readiness check validates Ubuntu version, CPU architecture, memory, disk, required release files, Docker/Compose availability, port `8501`, absence of repo `.env`, required non-placeholder DB secrets, and Testnet credentials when `ENABLE_REAL_TESTNET_ORDERS=true`.

## 4. Install

```bash
sudo bash scripts/install_ubuntu.sh --env-file /root/horizon-prod.env
```

The installer:

- installs Docker Engine and Docker Compose plugin
- creates `/opt/horizon-lab`
- copies this release bundle
- installs the private env file as `/opt/horizon-lab/.env` with `0600` permissions
- installs `horizon-backend.service` and `horizon-ui.service`
- enables backend service

## 5. Start Headless Backend

```bash
sudo systemctl start horizon-backend
sudo systemctl status horizon-backend --no-pager
```

The backend runs without UI:

- MariaDB
- Redis
- worker-marketdata
- worker-signal
- worker-risk
- worker-ml
- worker-order
- worker-pnl

## 6. Optional UI

```bash
sudo systemctl start horizon-ui
```

Open:

```text
http://<droplet-ip>:8501
```

For public internet use, restrict access with firewall rules, SSH tunnel, or a reverse proxy with authentication.

## 7. Health Check

```bash
cd /opt/horizon-lab
bash scripts/healthcheck_ubuntu.sh
```

## 8. Logs

```bash
docker compose -f docker-compose.prod.yml --env-file .env logs -f worker-signal
docker compose -f docker-compose.prod.yml --env-file .env logs -f worker-ml
docker compose -f docker-compose.prod.yml --env-file .env logs -f worker-order
```

## 9. Database

MariaDB schema is bootstrapped from:

```text
sql/init/001_schema.sql
```

The app also runs idempotent schema initialization at startup.

## 10. Stop

```bash
sudo systemctl stop horizon-ui
sudo systemctl stop horizon-backend
```

## 11. Update Release

```bash
cd /opt/horizon-lab
git pull
sudo systemctl restart horizon-backend
sudo systemctl restart horizon-ui
```

## 12. Secret Hygiene

- Commit only `.env.example` and `.env.production.example`; both must contain blank or placeholder values only.
- Never commit `.env`, `.env.production`, `.env.staging`, or any environment-specific file.
- `.gitignore` blocks `.env.*` by default and only allows the checked-in example templates.
- Docker builds exclude `.env`, so secrets are passed at runtime through Compose env files only.
