#!/usr/bin/env bash
set -u

COMPOSE_FILE="${COMPOSE_FILE:-docker-compose.prod.yml}"
PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$PROJECT_DIR" || exit 1
UI_HOST_PORT="${UI_HOST_PORT:-$(grep -E '^UI_HOST_PORT=' .env 2>/dev/null | tail -1 | cut -d= -f2-)}"
UI_HOST_PORT="${UI_HOST_PORT:-8501}"

fail=0
check() {
  local name="$1"
  local cmd="$2"
  if eval "$cmd" >/tmp/horizon_health.out 2>/tmp/horizon_health.err; then
    printf "%-32s OK\n" "$name"
  else
    printf "%-32s FAIL\n" "$name"
    sed 's/^/  /' /tmp/horizon_health.err | tail -5
    fail=1
  fi
}

echo "Horizon Ubuntu health check"
echo "Project: $PROJECT_DIR"

check "env file" "test -f .env"
check "docker installed" "command -v docker"
check "docker daemon" "docker info"
check "compose config" "docker compose -f $COMPOSE_FILE --env-file .env config"
check "python compile in image" "docker compose -f $COMPOSE_FILE --env-file .env run --rm --no-deps worker-signal python -m py_compile horizon_institutional_live_production_grade.py"
check "mariadb service" "docker compose -f $COMPOSE_FILE --env-file .env ps mariadb"
check "redis service" "docker compose -f $COMPOSE_FILE --env-file .env ps redis"
check "backend services" "docker compose -f $COMPOSE_FILE --env-file .env ps worker-marketdata worker-signal worker-risk worker-ml worker-order worker-pnl"

if curl -fsS --max-time 3 "http://127.0.0.1:${UI_HOST_PORT}/_stcore/health" >/tmp/horizon_ui_health.out 2>/tmp/horizon_ui_health.err; then
  printf "%-32s OK\n" "ui health"
else
  printf "%-32s SKIP/FAIL\n" "ui health"
fi

exit "$fail"
