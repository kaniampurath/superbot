#!/usr/bin/env bash
set -euo pipefail

ACTION="${1:-health}"
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
COMPOSE_FILE="${COMPOSE_FILE:-docker-compose.prod.yml}"
ENV_FILE="${ENV_FILE:-.env}"
cd "$ROOT"
UI_HOST_PORT="${UI_HOST_PORT:-$(grep -E '^UI_HOST_PORT=' "$ENV_FILE" 2>/dev/null | tail -1 | cut -d= -f2-)}"
UI_HOST_PORT="${UI_HOST_PORT:-8501}"

compose() {
  docker compose -f "$COMPOSE_FILE" --env-file "$ENV_FILE" "$@"
}

docker_ready() {
  command -v docker >/dev/null 2>&1 && docker info >/dev/null 2>&1
}

case "$ACTION" in
  start-backend)
    compose up -d --build
    compose ps
    ;;
  start-ui)
    compose --profile ui up -d --build
    compose --profile ui ps
    ;;
  health)
    bash scripts/healthcheck_ubuntu.sh
    ;;
  status)
    if docker_ready; then
      compose ps
    else
      echo "Docker daemon: unavailable"
    fi
    if curl -fsS --max-time 3 "http://127.0.0.1:${UI_HOST_PORT}/_stcore/health" >/dev/null; then
      echo "UI health: ok"
    else
      echo "UI health: unavailable"
    fi
    ;;
  logs)
    compose --profile ui logs --tail "${TAIL_LINES:-120}" "${@:2}"
    ;;
  db)
    compose exec mariadb sh -lc 'mariadb -u"$MYSQL_USER" -p"$MYSQL_PASSWORD" "$MYSQL_DATABASE"'
    ;;
  redis)
    compose exec redis redis-cli
    ;;
  troubleshoot)
    bash scripts/troubleshoot_ubuntu.sh
    ;;
  performance)
    if docker_ready; then
      compose run --rm --no-deps worker-signal python scripts/performance_report.py --env-file "$ENV_FILE"
    else
      python scripts/performance_report.py --env-file "$ENV_FILE"
    fi
    ;;
  performance-json)
    if docker_ready; then
      compose run --rm --no-deps worker-signal python scripts/performance_report.py --env-file "$ENV_FILE" --json
    else
      python scripts/performance_report.py --env-file "$ENV_FILE" --json
    fi
    ;;
  validate-once)
    if docker_ready; then
      compose run --rm --no-deps worker-validation python horizon_institutional_live_production_grade.py validation-once
    else
      RUN_MODE=validation-once python horizon_institutional_live_production_grade.py
    fi
    ;;
  test-headless)
    if docker_ready; then
      compose run --rm --no-deps worker-validation python scripts/headless_functional_test.py
    else
      python scripts/headless_functional_test.py
    fi
    ;;
  migrate-db)
    if docker_ready; then
      compose up -d mariadb redis
      compose build worker-signal
      compose run --rm --no-deps worker-signal python horizon_institutional_live_production_grade.py migrate-db
    else
      python horizon_institutional_live_production_grade.py migrate-db
    fi
    ;;
  stop)
    compose --profile ui down
    ;;
  *)
    echo "Usage: scripts/horizonctl.sh {start-backend|start-ui|health|status|logs|db|redis|troubleshoot|performance|performance-json|validate-once|test-headless|migrate-db|stop}"
    exit 2
    ;;
esac
