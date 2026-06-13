#!/usr/bin/env bash
set -euo pipefail

ACTION="${1:-health}"
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
COMPOSE_FILE="${COMPOSE_FILE:-docker-compose.prod.yml}"
ENV_FILE="${ENV_FILE:-.env}"
cd "$ROOT"

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
    if curl -fsS --max-time 3 http://127.0.0.1:8501/_stcore/health >/dev/null; then
      echo "UI health: ok"
    else
      echo "UI health: unavailable"
    fi
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
  stop)
    compose --profile ui down
    ;;
  *)
    echo "Usage: scripts/horizonctl.sh {start-backend|start-ui|health|status|performance|performance-json|stop}"
    exit 2
    ;;
esac
