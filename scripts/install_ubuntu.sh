#!/usr/bin/env bash
set -euo pipefail

APP_DIR="${APP_DIR:-/opt/horizon-lab}"
APP_USER="${APP_USER:-horizon}"
REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
ENV_SOURCE="${ENV_SOURCE:-}"
CHECK_ONLY=false

usage() {
  cat <<'USAGE'
Usage:
  sudo bash scripts/install_ubuntu.sh [options]

Options:
  --check                 Check Ubuntu droplet readiness only; do not install.
  --env-file PATH         Copy an environment-specific env file to /opt/horizon-lab/.env.
  --app-dir PATH          Install location. Default: /opt/horizon-lab.
  --app-user USER         System user owner. Default: horizon.
  -h, --help              Show help.

Secrets policy:
  Keep real secrets outside git. Store a private env file on the droplet, for example
  /root/horizon-prod.env, then install with:
    sudo bash scripts/install_ubuntu.sh --env-file /root/horizon-prod.env
USAGE
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --check)
      CHECK_ONLY=true
      shift
      ;;
    --env-file)
      ENV_SOURCE="${2:-}"
      if [[ -z "$ENV_SOURCE" ]]; then
        echo "--env-file requires a path."
        exit 2
      fi
      shift 2
      ;;
    --app-dir)
      APP_DIR="${2:-}"
      if [[ -z "$APP_DIR" ]]; then
        echo "--app-dir requires a path."
        exit 2
      fi
      shift 2
      ;;
    --app-user)
      APP_USER="${2:-}"
      if [[ -z "$APP_USER" ]]; then
        echo "--app-user requires a user name."
        exit 2
      fi
      shift 2
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    *)
      echo "Unknown option: $1"
      usage
      exit 2
      ;;
  esac
done

check_status=0
check_ok() {
  printf "%-34s OK\n" "$1"
}

check_fail() {
  printf "%-34s FAIL %s\n" "$1" "$2"
  check_status=1
}

require_file() {
  local path="$1"
  local label="$2"
  if [[ -f "$path" ]]; then
    check_ok "$label"
  else
    check_fail "$label" "missing: $path"
  fi
}

check_env_file() {
  local env_file="$1"
  local label="$2"
  local required_vars=(MYSQL_PASSWORD MYSQL_ROOT_PASSWORD)
  local var testnet_enabled has_testnet_key has_testnet_secret
  if [[ ! -f "$env_file" ]]; then
    check_fail "$label" "missing: $env_file"
    return
  fi
  check_ok "$label"
  for var in "${required_vars[@]}"; do
    if grep -Eq "^${var}=.+" "$env_file" && ! grep -Eq "^${var}=change_me|^${var}=root_password|^${var}=horizon_password" "$env_file"; then
      check_ok "env ${var}"
    else
      check_fail "env ${var}" "must be set to a non-placeholder value"
    fi
  done
  testnet_enabled="$(grep -E '^ENABLE_REAL_TESTNET_ORDERS=' "$env_file" | tail -1 | cut -d= -f2- | tr '[:upper:]' '[:lower:]' || true)"
  if [[ "$testnet_enabled" == "true" || "$testnet_enabled" == "1" || "$testnet_enabled" == "yes" || "$testnet_enabled" == "on" ]]; then
    has_testnet_key=false
    has_testnet_secret=false
    grep -Eq '^(testnet_key|TESTNET_KEY|BINANCE_TESTNET_API_KEY)=.+' "$env_file" && has_testnet_key=true
    grep -Eq '^(testnet_secret|TESTNET_SECRET|BINANCE_TESTNET_API_SECRET)=.+' "$env_file" && has_testnet_secret=true
    [[ "$has_testnet_key" == "true" ]] && check_ok "env testnet key" || check_fail "env testnet key" "required when ENABLE_REAL_TESTNET_ORDERS=true"
    [[ "$has_testnet_secret" == "true" ]] && check_ok "env testnet secret" || check_fail "env testnet secret" "required when ENABLE_REAL_TESTNET_ORDERS=true"
  fi
}

run_readiness_check() {
  echo "Horizon Ubuntu readiness check"
  echo "Repo: $REPO_DIR"
  echo "App dir: $APP_DIR"

  if [[ -f /etc/os-release ]]; then
    . /etc/os-release
    case "${ID:-}" in
      ubuntu) check_ok "os ubuntu" ;;
      *) check_fail "os ubuntu" "detected ${PRETTY_NAME:-unknown}" ;;
    esac
    case "${VERSION_ID:-}" in
      22.04|24.04) check_ok "ubuntu lts version" ;;
      *) check_fail "ubuntu lts version" "expected 22.04 or 24.04, detected ${VERSION_ID:-unknown}" ;;
    esac
  else
    check_fail "os release" "/etc/os-release not found"
  fi

  case "$(uname -m)" in
    x86_64|aarch64) check_ok "cpu architecture" ;;
    *) check_fail "cpu architecture" "unsupported: $(uname -m)" ;;
  esac

  local mem_mb disk_mb
  mem_mb="$(awk '/MemTotal/ {print int($2/1024)}' /proc/meminfo 2>/dev/null || echo 0)"
  disk_mb="$(df -Pm "$(dirname "$APP_DIR")" 2>/dev/null | awk 'NR==2 {print $4}' || echo 0)"
  [[ "$mem_mb" -ge 1900 ]] && check_ok "memory >= 2GB" || check_fail "memory >= 2GB" "detected ${mem_mb}MB"
  [[ "$disk_mb" -ge 10240 ]] && check_ok "free disk >= 10GB" || check_fail "free disk >= 10GB" "detected ${disk_mb}MB"

  command -v curl >/dev/null 2>&1 && check_ok "curl installed" || check_fail "curl installed" "missing"
  command -v git >/dev/null 2>&1 && check_ok "git installed" || check_fail "git installed" "missing"
  command -v rsync >/dev/null 2>&1 && check_ok "rsync installed" || check_fail "rsync installed" "missing"
  command -v docker >/dev/null 2>&1 && check_ok "docker installed" || check_fail "docker installed" "missing; installer can add it"
  docker compose version >/dev/null 2>&1 && check_ok "docker compose plugin" || check_fail "docker compose plugin" "missing; installer can add it"

  if ss -ltn 2>/dev/null | awk '{print $4}' | grep -Eq '(:8501)$'; then
    check_fail "port 8501 available" "already in use"
  else
    check_ok "port 8501 available"
  fi

  require_file "$REPO_DIR/Dockerfile" "Dockerfile"
  require_file "$REPO_DIR/docker-compose.prod.yml" "production compose"
  require_file "$REPO_DIR/sql/init/001_schema.sql" "database init script"
  require_file "$REPO_DIR/.env.production.example" "env template"

  if [[ -n "$ENV_SOURCE" ]]; then
    check_env_file "$ENV_SOURCE" "external env file"
  elif [[ -f "$APP_DIR/.env" ]]; then
    check_env_file "$APP_DIR/.env" "installed env file"
  else
    check_fail "external env file" "recommended: pass --env-file /root/horizon-prod.env; installer can still create a placeholder .env"
  fi

  if [[ -f "$REPO_DIR/.env" ]]; then
    check_fail "repo .env absent" "real .env must not be stored in the repo"
  else
    check_ok "repo .env absent"
  fi

  return "$check_status"
}

if [[ "$CHECK_ONLY" == "true" ]]; then
  run_readiness_check
  exit "$?"
fi

if [[ "$(id -u)" -ne 0 ]]; then
  echo "Run as root: sudo bash scripts/install_ubuntu.sh"
  exit 1
fi

if [[ -n "$ENV_SOURCE" && ! -f "$ENV_SOURCE" ]]; then
  echo "Environment file not found: $ENV_SOURCE"
  exit 1
fi

install_docker_engine() {
  install -m 0755 -d /etc/apt/keyrings
  rm -f /etc/apt/keyrings/docker.gpg
  curl -fsSL https://download.docker.com/linux/ubuntu/gpg | gpg --dearmor -o /etc/apt/keyrings/docker.gpg
  chmod a+r /etc/apt/keyrings/docker.gpg
  . /etc/os-release
  echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.gpg] https://download.docker.com/linux/ubuntu ${VERSION_CODENAME} stable" > /etc/apt/sources.list.d/docker.list
  apt-get update
  apt-get install -y docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin
}

apt-get update
apt-get install -y ca-certificates curl gnupg git rsync ufw

if ! command -v docker >/dev/null 2>&1 || ! docker compose version >/dev/null 2>&1; then
  install_docker_engine
fi

systemctl enable --now docker

id -u "$APP_USER" >/dev/null 2>&1 || useradd --system --create-home --shell /usr/sbin/nologin "$APP_USER"
usermod -aG docker "$APP_USER" || true

mkdir -p "$APP_DIR"
if [[ "$(realpath "$REPO_DIR")" != "$(realpath "$APP_DIR")" ]]; then
  rsync -a --delete --exclude ".git" --exclude "__pycache__" --exclude ".pycache_check" "$REPO_DIR"/ "$APP_DIR"/
else
  echo "Repository is already in $APP_DIR; skipping self-copy."
fi
chown -R "$APP_USER":"$APP_USER" "$APP_DIR"

if [[ -n "$ENV_SOURCE" ]]; then
  install -m 0600 -o "$APP_USER" -g "$APP_USER" "$ENV_SOURCE" "$APP_DIR/.env"
  echo "Installed environment file from $ENV_SOURCE to $APP_DIR/.env."
elif [[ ! -f "$APP_DIR/.env" ]]; then
  install -m 0600 -o "$APP_USER" -g "$APP_USER" "$APP_DIR/.env.production.example" "$APP_DIR/.env"
  chown "$APP_USER":"$APP_USER" "$APP_DIR/.env"
  echo "Created placeholder $APP_DIR/.env from template. Replace placeholders before starting production."
fi

install -m 0644 "$APP_DIR/systemd/horizon-backend.service" /etc/systemd/system/horizon-backend.service
install -m 0644 "$APP_DIR/systemd/horizon-ui.service" /etc/systemd/system/horizon-ui.service
systemctl daemon-reload
systemctl enable horizon-backend.service

ufw allow OpenSSH >/dev/null || true
ufw allow 8501/tcp >/dev/null || true

echo "Install complete."
echo "1) Verify env: sudo bash $APP_DIR/scripts/install_ubuntu.sh --check --env-file $APP_DIR/.env"
echo "2) Start backend: sudo systemctl start horizon-backend"
echo "3) Optional UI: sudo systemctl start horizon-ui"
echo "4) Health: cd $APP_DIR && bash scripts/healthcheck_ubuntu.sh"
