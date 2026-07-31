#!/usr/bin/env bash
# =============================================================================
# AgentTrust Deploy Script
# =============================================================================
#
# USAGE:
#   ./scripts/deploy.sh              # pull, build, migrate, restart, health-check
#   ./scripts/deploy.sh --no-pull    # same, but deploy the working tree as-is
#
# HOW IT WORKS:
#   One app container on 127.0.0.1:8140, published by a Cloudflare Tunnel.
#   Restarting it drops requests for a few seconds; the tunnel retries through
#   the gap. If the new container fails its health check the deploy aborts
#   loudly — but the old container is already gone, so check the logs and fix
#   forward, or roll back.
#
# ROLLBACK:
#   git checkout <last-good-sha> && ./scripts/deploy.sh --no-pull
#
# REQUIREMENTS:
#   - Colima running (`colima status`) with the docker CLI + compose plugin
#   - docker-buildx (`brew install docker-buildx`) — the Dockerfile uses
#     `RUN --mount=type=cache`, which the classic builder cannot parse
#   - .env.prod present in the repo root (NOT .env — see ENV_FILE below)
#   - keys/service.key present — the attestation signing key, copied from the old
#     host. Never regenerate it: a new key invalidates every issued attestation.
#   - the `observability` Docker network (docker network create observability)
#
# =============================================================================
set -euo pipefail

cd "$(dirname "${BASH_SOURCE[0]}")/.."

COMPOSE_FILE="docker-compose.prod.yml"
# --env-file and `env_file:` are different things: the flag feeds ${VAR}
# interpolation in the compose file, the directive feeds the containers. Both are
# needed, and both must point at .env.prod.
ENV_FILE=".env.prod"
HEALTH_URL="http://127.0.0.1:8140/health"

HEALTH_INTERVAL=2 # seconds between health check polls
HEALTH_TIMEOUT=60 # total seconds to wait for healthy response

# Colima's docker daemon still defaults to the classic builder, which chokes on
# the Dockerfile's `RUN --mount=type=cache` with a bare "requires BuildKit".
export DOCKER_BUILDKIT=1

# -----------------------------------------------------------------------------
# ANSI color helpers
# -----------------------------------------------------------------------------
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
CYAN='\033[0;36m'
BOLD='\033[1m'
RESET='\033[0m'

log_info()    { echo -e "${CYAN}[$(date '+%Y-%m-%d %H:%M:%S')] [INFO]${RESET}  $*"; }
log_success() { echo -e "${GREEN}[$(date '+%Y-%m-%d %H:%M:%S')] [OK]${RESET}    $*"; }
log_warn()    { echo -e "${YELLOW}[$(date '+%Y-%m-%d %H:%M:%S')] [WARN]${RESET}  $*"; }
log_error()   { echo -e "${RED}[$(date '+%Y-%m-%d %H:%M:%S')] [ERROR]${RESET} $*" >&2; }

compose() { docker compose --env-file "${ENV_FILE}" -f "${COMPOSE_FILE}" "$@"; }

# Poll /health until HTTP 200 or timeout. Returns 0 on success, 1 on timeout.
wait_for_healthy() {
  local elapsed=0

  log_info "Waiting for ${HEALTH_URL} (timeout: ${HEALTH_TIMEOUT}s)"

  while (( elapsed < HEALTH_TIMEOUT )); do
    local http_code
    http_code="$(curl -s -o /dev/null -w '%{http_code}' --connect-timeout 2 "${HEALTH_URL}" 2>/dev/null || true)"
    if [[ "${http_code}" == "200" ]]; then
      log_success "App is healthy (HTTP 200) after ${elapsed}s"
      return 0
    fi
    log_info "  attempt ${elapsed}s — got HTTP ${http_code:-???}, retrying in ${HEALTH_INTERVAL}s…"
    sleep "${HEALTH_INTERVAL}"
    (( elapsed += HEALTH_INTERVAL ))
  done

  log_error "App did not become healthy within ${HEALTH_TIMEOUT}s"
  return 1
}

main() {
  local pull=true
  case "${1:-}" in
    --no-pull) pull=false ;;
    '')        ;;
    *)         log_error "Unknown argument: $1"; echo "Usage: $0 [--no-pull]"; exit 1 ;;
  esac

  echo -e "\n${BOLD}${CYAN}════════════════════════════════════════${RESET}"
  echo -e "${BOLD}${CYAN}  AgentTrust — Deploy${RESET}"
  echo -e "${BOLD}${CYAN}════════════════════════════════════════${RESET}\n"

  if [[ ! -f "${ENV_FILE}" ]]; then
    log_error "${ENV_FILE} not found in $(pwd) — copy it from the password manager first."
    log_error "It is deliberately not named .env: the dev server and test suite read .env,"
    log_error "and they must not pick up production secrets on this machine."
    exit 1
  fi

  # 1. Pull latest code
  if [[ "${pull}" == true ]]; then
    log_info "Pulling latest code from origin/main…"
    git pull origin main
    log_success "git pull done"
  else
    log_warn "Skipping git pull — deploying the working tree as-is"
  fi

  # 2. Build the image
  log_info "Building Docker image…"
  compose build
  log_success "Image built"

  # 3. Run database migrations before anything starts against the new schema
  log_info "Running Alembic migrations…"
  compose run --rm agent-trust uv run alembic upgrade head
  log_success "Migrations applied"

  # 4. Recreate the app and workers on the new image
  log_info "Starting services…"
  compose up -d
  log_success "Containers started"

  # 5. Health check
  if wait_for_healthy; then
    log_info "Pruning old images…"
    docker image prune -f
    log_success "Image prune done"

    echo -e "\n${GREEN}${BOLD}✓ Deploy complete — serving on 127.0.0.1:8140${RESET}\n"
  else
    log_error "Deploy failed: the app is not answering /health."
    log_error "Logs:      make prod-logs   (or: docker compose --env-file ${ENV_FILE} -f ${COMPOSE_FILE} logs --tail=50 agent-trust)"
    log_error "Roll back: git checkout <last-good-sha> && ./scripts/deploy.sh --no-pull"
    exit 1
  fi
}

main "$@"
