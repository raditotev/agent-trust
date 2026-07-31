.PHONY: help install keygen migrate lint format format-check test test-engine test-auth test-tools test-integration \
        server server-http dev ci docker-up docker-down docker-build \
        deploy prod-up prod-down prod-logs prod-ps

# Default target
help:
	@echo "Usage: make <target>"
	@echo ""
	@echo "Setup"
	@echo "  install        Install dependencies (uv sync)"
	@echo "  keygen         Generate Ed25519 signing keypair (first time only)"
	@echo "  migrate        Run database migrations"
	@echo ""
	@echo "Code quality"
	@echo "  lint           Run ruff linter"
	@echo "  format         Auto-format source with ruff"
	@echo "  format-check   Check formatting without modifying files"
	@echo ""
	@echo "Testing"
	@echo "  test           Run all tests"
	@echo "  test-engine    Run score engine tests"
	@echo "  test-auth      Run auth layer tests"
	@echo "  test-tools     Run MCP tool tests"
	@echo "  test-integration Run end-to-end integration tests"
	@echo ""
	@echo "Server"
	@echo "  server         Run MCP server (stdio)"
	@echo "  server-http    Run MCP server (streamable-http, port 8000)"
	@echo "  dev            Open MCP Inspector"
	@echo ""
	@echo "Docker (development stack)"
	@echo "  docker-up      Start postgres + redis"
	@echo "  docker-down    Stop all docker services"
	@echo "  docker-build   Build docker images"
	@echo ""
	@echo "Production (this machine — see docs/deployment.md)"
	@echo "  deploy         Build, migrate, restart, health-check"
	@echo "  prod-up        Start the serving stack (assumes a prior build)"
	@echo "  prod-down      Stop the serving stack (volumes are kept)"
	@echo "  prod-logs      Tail the serving stack's logs"
	@echo "  prod-ps        Show the serving stack's containers"
	@echo ""
	@echo "CI"
	@echo "  ci             Run full CI pipeline (lint, format-check, migrate, test)"

# ── Setup ─────────────────────────────────────────────────────────────────────

install:
	uv sync

keygen:
	uv run python scripts/generate_keypair.py

migrate:
	uv run alembic upgrade head

# ── Code quality ──────────────────────────────────────────────────────────────

lint:
	uv run ruff check src/

format:
	uv run ruff format src/

format-check:
	uv run ruff format --check src/

# ── Testing ───────────────────────────────────────────────────────────────────

test:
	uv run pytest --tb=short -q

test-engine:
	uv run pytest tests/test_engine/ -v

test-auth:
	uv run pytest tests/test_auth/ -v

test-tools:
	uv run pytest tests/test_tools/ -v

test-integration:
	uv run pytest tests/test_integration/ -v

# ── Server ────────────────────────────────────────────────────────────────────

server:
	uv run python -m agent_trust.server

server-http:
	uv run python -m agent_trust.server --transport streamable-http --port 8000

dev:
	uv run mcp dev src/agent_trust/server.py

# ── Docker (development stack: docker-compose.yml, project agent-trust-dev) ───

docker-up:
	docker compose up -d postgres redis

docker-down:
	docker compose down

docker-build:
	docker compose build

# ── Production (serving stack: docker-compose.prod.yml, project agent-trust) ──
#
# --env-file and the compose file's `env_file:` are different mechanisms — the
# flag feeds ${VAR} interpolation, the directive feeds the containers — so both
# are needed and both point at .env.prod. Not .env: pydantic-settings and pytest
# read that one, and production secrets there make the test suite run against
# production.
PROD_COMPOSE = docker compose --env-file .env.prod -f docker-compose.prod.yml

# Colima's daemon defaults to the classic builder, which cannot parse the
# Dockerfile's `RUN --mount=type=cache`. Needs `brew install docker-buildx`.
export DOCKER_BUILDKIT = 1

deploy:
	./scripts/deploy.sh

prod-up:
	$(PROD_COMPOSE) up -d

prod-down:
	$(PROD_COMPOSE) down

prod-logs:
	$(PROD_COMPOSE) logs -f --tail=100

prod-ps:
	$(PROD_COMPOSE) ps

# ── CI ────────────────────────────────────────────────────────────────────────

ci: install keygen migrate lint format-check test
