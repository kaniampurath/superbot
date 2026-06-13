#!/usr/bin/env bash
set -u

COMPOSE_FILE="${COMPOSE_FILE:-docker-compose.prod.yml}"
ENV_FILE="${ENV_FILE:-.env}"
TAIL_LINES="${TAIL_LINES:-80}"
PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$PROJECT_DIR" || exit 1

compose() {
  docker compose -f "$COMPOSE_FILE" --env-file "$ENV_FILE" "$@"
}

section() {
  printf "\n== %s ==\n" "$1"
}

run() {
  local title="$1"
  shift
  printf "\n-- %s --\n" "$title"
  "$@" 2>&1 || true
}

compose_exec() {
  compose exec -T "$@" 2>&1 || true
}

env_value() {
  grep -E "^$1=" "$ENV_FILE" 2>/dev/null | tail -1 | cut -d= -f2-
}

redacted_env() {
  for key in RUN_MODE SYSTEM_STAGE SYMBOLS KLINE_INTERVAL UI_HOST_PORT MYSQL_HOST MYSQL_PORT MYSQL_DATABASE MYSQL_USER REDIS_HOST REDIS_PORT ENABLE_REAL_TESTNET_ORDERS TRAINING_AUTO_APPROVE_PAPER DEPLOYMENT_UNLOCKED; do
    value="$(env_value "$key")"
    if [[ -n "$value" ]]; then
      printf "%s=%s\n" "$key" "$value"
    fi
  done
  for key in MYSQL_PASSWORD MYSQL_ROOT_PASSWORD testnet_key testnet_secret BINANCE_TESTNET_API_KEY BINANCE_TESTNET_API_SECRET; do
    value="$(env_value "$key")"
    if [[ -n "$value" && "$value" != *change-me* && "$value" != *REPLACE* ]]; then
      printf "%s=<set>\n" "$key"
    else
      printf "%s=<missing-or-placeholder>\n" "$key"
    fi
  done
}

sql() {
  local query="$1"
  printf "%s\n" "$query" | compose exec -T mariadb sh -lc 'mariadb -u"$MYSQL_USER" -p"$MYSQL_PASSWORD" "$MYSQL_DATABASE" --table' 2>&1 || true
}

echo "Horizon droplet troubleshooting report"
echo "Project: $PROJECT_DIR"
echo "Compose: $COMPOSE_FILE"
echo "Env: $ENV_FILE"
echo "Tail lines: $TAIL_LINES"
date -u +"UTC: %Y-%m-%d %H:%M:%S"

section "Git Deployment State"
run "current commit" git log -1 --oneline
run "branch" git branch --show-current
run "remote" git remote -v
run "status" git status --short

section "Redacted Runtime Config"
redacted_env

section "Host Capacity"
run "os" sh -lc "cat /etc/os-release 2>/dev/null | sed -n '1,6p'"
run "disk" df -h /
run "memory" free -h
run "ports" sh -lc "ss -ltnp 2>/dev/null | grep -E ':8501|:8502|:3306|:3307|:6379' || true"

section "Systemd Units"
run "horizon units" sh -lc "systemctl list-units --type=service --all | grep -Ei 'horizon|superbot|trading|bot|mysql|maria|redis' || true"
run "backend status" systemctl status horizon-backend --no-pager
run "ui status" systemctl status horizon-ui --no-pager

section "Docker Compose"
run "docker version" docker --version
run "compose version" docker compose version
run "compose config" compose config
run "compose ps" compose ps
run "containers" docker ps --format "table {{.ID}}\t{{.Names}}\t{{.Image}}\t{{.Status}}\t{{.Ports}}"

section "Service Logs"
run "backend worker logs" compose logs --tail "$TAIL_LINES" worker-marketdata worker-validation worker-signal worker-risk worker-ml worker-order worker-pnl
run "ui logs" compose --profile ui logs --tail "$TAIL_LINES" horizon-ui
run "mariadb logs" compose logs --tail "$TAIL_LINES" mariadb
run "redis logs" compose logs --tail "$TAIL_LINES" redis

section "Redis State"
run "redis ping" compose_exec redis redis-cli ping
run "worker keys" compose_exec redis redis-cli --scan --pattern "worker_status:*"
run "risk/drift/pnl keys" compose_exec redis sh -lc "redis-cli GET risk_state; redis-cli GET drift_state; redis-cli GET live_pnl"

section "MariaDB State"
run "db ping" compose_exec mariadb sh -lc 'mariadb-admin -u"$MYSQL_USER" -p"$MYSQL_PASSWORD" ping'
run "table counts" sql "SELECT 'signals' table_name, COUNT(*) rows_count FROM signals UNION ALL SELECT 'deployment_requests', COUNT(*) FROM deployment_requests UNION ALL SELECT 'paper_orders', COUNT(*) FROM paper_orders UNION ALL SELECT 'testnet_orders', COUNT(*) FROM testnet_orders UNION ALL SELECT 'positions', COUNT(*) FROM positions UNION ALL SELECT 'executions', COUNT(*) FROM executions UNION ALL SELECT 'feature_snapshots', COUNT(*) FROM feature_snapshots UNION ALL SELECT 'trade_outcomes', COUNT(*) FROM trade_outcomes UNION ALL SELECT 'model_registry', COUNT(*) FROM model_registry UNION ALL SELECT 'audit_log', COUNT(*) FROM audit_log;"
run "worker heartbeat" sql "SELECT worker_name, status, host, last_seen FROM worker_heartbeat ORDER BY worker_name;"
run "active model" sql "SELECT model_name, version, status, trained_rows, trained_at, LEFT(metrics_json, 220) metrics FROM model_registry ORDER BY trained_at DESC LIMIT 5;"
run "recent signals" sql "SELECT id, symbol, side, deployable, ROUND(composite_score,4) score, ROUND(ml_confidence,4) ml_conf, ml_model_version, ts FROM signals ORDER BY id DESC LIMIT 10;"
run "open positions" sql "SELECT id, symbol, side, ROUND(entry_price,4) entry_price, ROUND(current_price,4) current_price, ROUND(unrealized_pnl,4) unrealized_pnl, status, updated_at FROM positions WHERE status='OPEN' ORDER BY updated_at DESC LIMIT 10;"
run "recent audit" sql "SELECT id, event_type, actor, symbol, LEFT(message, 180) message, ts FROM audit_log ORDER BY id DESC LIMIT 12;"

section "Application Health And Performance"
run "healthcheck" bash scripts/healthcheck_ubuntu.sh
run "performance" bash scripts/horizonctl.sh performance
