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

The readiness check validates Ubuntu version, CPU architecture, memory, disk, required release files, Docker/Compose availability, configured `UI_HOST_PORT`, absence of repo `.env`, required non-placeholder DB secrets, and Testnet credentials when `ENABLE_REAL_TESTNET_ORDERS=true`.

Before either `--check` or install mode, the script also inspects any existing setup: app directory, git checkout, installed env file, source env file, systemd backend/UI units, Docker daemon, Compose service status, and the configured `UI_HOST_PORT`.

## 4. Install

```bash
sudo bash scripts/install_ubuntu.sh --env-file /root/horizon-prod.env
```

The installer:

- installs Docker Engine and Docker Compose plugin
- creates `/opt/horizon-lab`
- copies this release bundle
- installs the private env file as `/opt/horizon-lab/.env` with `0600` permissions
- installs `horizon-backend.service` and `horizon-ui.service` with `WorkingDirectory` set to the chosen `--app-dir`
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

If port `8501` is already in use, set this in your private env file before install:

```text
UI_HOST_PORT=8502
```

Then open:

```text
http://<droplet-ip>:8502
```

For public internet use, restrict access with firewall rules, SSH tunnel, or a reverse proxy with authentication.

## 7. Health Check

```bash
cd /opt/horizon-lab
bash scripts/healthcheck_ubuntu.sh
```

## 8. Performance From CLI

The UI is optional. Backend performance can be checked from the prompt:

```bash
cd /home/myts/superbot
bash scripts/horizonctl.sh performance
bash scripts/horizonctl.sh performance-json
bash scripts/horizonctl.sh validate-once
bash scripts/horizonctl.sh test-headless
```

The report includes equity, daily P&L, realized/unrealized P&L, drawdown, risk state, drift state, worker heartbeats, order counts, open positions, active ML model metrics, recent signals, recent orders, validation state, and recent handoffs. `test-headless` verifies that the backend can run without Streamlit while preserving validation, journal, handoff, and order-gate data.

## 9. Logs

```bash
docker compose -f docker-compose.prod.yml --env-file .env logs -f worker-signal
docker compose -f docker-compose.prod.yml --env-file .env logs -f worker-ml
docker compose -f docker-compose.prod.yml --env-file .env logs -f worker-order
```

## 10. Database

MariaDB schema is bootstrapped from:

```text
sql/init/001_schema.sql
```

The app also runs idempotent schema initialization at startup.

After pulling a release with database changes, run:

```bash
cd /home/myts/superbot
bash scripts/horizonctl.sh migrate-db
bash scripts/horizonctl.sh test-headless
```

## 11. Stop

```bash
sudo systemctl stop horizon-ui
sudo systemctl stop horizon-backend
```

## 12. Update Release

```bash
cd /opt/horizon-lab
git pull
sudo systemctl restart horizon-backend
sudo systemctl restart horizon-ui
```

## 13. Secret Hygiene

- Commit only `.env.example` and `.env.production.example`; both must contain blank or placeholder values only.
- Never commit `.env`, `.env.production`, `.env.staging`, or any environment-specific file.
- `.gitignore` blocks `.env.*` by default and only allows the checked-in example templates.
- Docker builds exclude `.env`, so secrets are passed at runtime through Compose env files only.
