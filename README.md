# Superbot Trading Lab

Superbot is a testnet-first trading lab. In plain English, it watches crypto market data, looks for short-term mean-reversion opportunities, checks whether each idea is safe enough to test, learns from trade outcomes, and records every important action.

The web dashboard is optional. The backend can run headless on an Ubuntu droplet through systemd workers, while the UI simply renders status, signals, charts, audit events, and install guidance.

![Superbot architecture diagram](docs/assets/architecture.svg)

## Quick Ubuntu Install

Use Ubuntu `24.04 LTS` for production. Keep real secrets out of GitHub.

```bash
sudo apt-get update
sudo apt-get install -y git

git clone https://github.com/kaniampurath/superbot.git /home/myts/superbot
cd /home/myts/superbot

cp .env.production.example horizon-prod.env
nano horizon-prod.env
chmod 600 horizon-prod.env

bash scripts/install_ubuntu.sh --check --app-dir /home/myts/superbot --app-user myts --env-file horizon-prod.env
sudo bash scripts/install_ubuntu.sh --app-dir /home/myts/superbot --app-user myts --env-file horizon-prod.env

sudo systemctl start horizon-backend
sudo systemctl start horizon-ui
bash scripts/healthcheck_ubuntu.sh
bash scripts/horizonctl.sh migrate-db
bash scripts/horizonctl.sh validate-once
bash scripts/horizonctl.sh test-headless
bash scripts/horizonctl.sh performance
```

## Required Config

| Setting | Purpose | Required |
|---|---|---|
| `MYSQL_PASSWORD` | Database password for the app user | Yes |
| `MYSQL_ROOT_PASSWORD` | MariaDB root password | Yes |
| `ENABLE_REAL_TESTNET_ORDERS` | Set `false` until Testnet credentials are ready | Yes |
| `testnet_key` / `testnet_secret` | Binance Spot Testnet credentials | Only when Testnet orders are enabled |
| `UI_HOST_PORT` | External Streamlit port, for example `8502` when `8501` is busy | No |

The installer checks the existing setup before installing: app directory, git checkout, env file, systemd services, Docker daemon, existing Compose services, and the configured `UI_HOST_PORT`.

## What Runs Headless

The default backend stack starts MariaDB, Redis, market data, validation/backtest, signal, risk, ML, order management, and P&L workers. The Streamlit UI is separate and optional.

```bash
docker compose -f docker-compose.prod.yml --env-file .env up -d --build
docker compose -f docker-compose.prod.yml --env-file .env --profile ui up -d horizon-ui
```

## See Performance

From Ubuntu CLI:

```bash
bash scripts/horizonctl.sh performance
bash scripts/horizonctl.sh performance-json
bash scripts/horizonctl.sh validate-once
bash scripts/horizonctl.sh test-headless
```

From the UI, open the Streamlit dashboard and switch to `Trading Dashboard`.

## Troubleshoot Directly On The Droplet

After a GitHub check-in, use the droplet as the source of truth for deployed runtime health:

```bash
cd /home/myts/superbot
git pull
sudo systemctl restart horizon-backend
sudo systemctl restart horizon-ui

bash scripts/horizonctl.sh status
bash scripts/horizonctl.sh health
bash scripts/horizonctl.sh migrate-db
bash scripts/horizonctl.sh validate-once
bash scripts/horizonctl.sh test-headless
bash scripts/horizonctl.sh performance
bash scripts/horizonctl.sh troubleshoot
```

Useful direct consoles:

```bash
bash scripts/horizonctl.sh logs
bash scripts/horizonctl.sh logs worker-validation worker-ml worker-signal worker-order
bash scripts/horizonctl.sh migrate-db
bash scripts/horizonctl.sh db
bash scripts/horizonctl.sh redis
```

`migrate-db` runs the app's idempotent schema bootstrap and migration list against MariaDB. Use it after pulling a release that adds or changes tables.

`test-headless` runs backend-only functional and performance checks. It proves validation/backtest can run without the UI, compact DB records and handoff events are produced, and training auto-approval is blocked unless validation is green.

`troubleshoot` prints git deployment state, redacted runtime config, host capacity, systemd status, Docker Compose state, recent service logs, Redis state, MariaDB counts, worker heartbeat, active model registry rows, recent signals, open positions, audit events, health, and performance. Secrets are reported only as `<set>` or `<missing-or-placeholder>`.

For the full operational runbook, see [README_RUN.md](README_RUN.md).
