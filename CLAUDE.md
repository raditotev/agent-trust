# AgentTrust — MCP Server for Reputation & Trust Scoring

## Quick Start

uv sync
docker compose up -d postgres redis
uv run alembic upgrade head
uv run python scripts/generate_keypair.py # first time only
uv run python scripts/register_scopes.py # register trust.\* scopes in AgentAuth

# Run server (stdio for local testing)

uv run python -m agent_trust.server

# Run server (Streamable HTTP for remote agents)

uv run python -m agent_trust.server --transport streamable-http --port 8000

# Test with MCP Inspector

uv run mcp dev src/agent_trust/server.py

## Running Tests

uv run pytest # all tests
uv run pytest tests/test_engine/ -v # score algorithm only
uv run pytest tests/test_auth/ -v # auth layer
uv run pytest tests/test_tools/ -v # MCP tool tests
uv run pytest tests/test_integration/ -v # end-to-end flows
uv run pytest --tb=short -q # quick summary

## Key Commands

uv run ruff check src/ # lint
uv run ruff format src/ # format
uv run alembic revision --autogenerate -m "msg" # new migration

## CI/CD

- Workflows: `.github/workflows/ci.yml` (lint + test) and `.github/workflows/deploy.yml` (SSH deploy)
- CI runs on every push/PR — postgres 16 + redis 7 services, full pytest suite
- CD fires on CI success on `main` — git pull → docker compose build → alembic upgrade head → docker compose up -d
- Server exposed via Cloudflare tunnel on the Hetzner host (no workflow changes needed for routing)
- Required secrets: `HETZNER_HOST`, `HETZNER_USER`, `HETZNER_SSH_KEY`, `HETZNER_APP_DIR`

## Architecture Rules

- This is an MCP-only server. NO REST API, NO HTTP endpoints beyond MCP transport.
- All agent-facing functionality is exposed as MCP tools, resources, or prompts.
- Tool docstrings are the API documentation — write them for LLM comprehension.
- AgentAuth is the primary identity provider. Token introspection via its MCP server.
- AgentTrust connects to AgentAuth as an MCP client (agentauth.radi.pro/mcp).
- Standalone Ed25519 is the fallback for agents not yet on AgentAuth.
- The auth layer is behind the AuthProvider protocol — adding new providers is
  a single class implementation.
- Score computation is async — interactions trigger background recomputation via arq.
- All interactions are immutable events — never update, only append.
- Attestations are Ed25519-signed JWTs with embedded score snapshots.

## AgentAuth Integration

- AgentAuth MCP URL: configured via AGENTAUTH_MCP_URL env var
- Token introspection: MCP tool `introspect_token(token)`
- Permission checks: MCP tool `check_permission(agent_id, action, resource, access_token)`
- Agent registration: MCP tool `quickstart(name, agent_type, description)`
- Trust scopes: trust.read, trust.report, trust.dispute.file,
  trust.dispute.resolve, trust.attest.issue, trust.admin
- AgentAuth MCP tool responses cached in Redis (max 5 min TTL)
- Auto-provision: if introspected agent_id not in local DB, create profile

## Environment Variables

- DATABASE_URL: postgres+asyncpg://... connection string
- REDIS_URL: redis://localhost:6379/0
- SIGNING_KEY_PATH: path to server Ed25519 private key (PEM)
- AUTH_PROVIDER: agentauth | standalone | both (default: both)
- AGENTAUTH_MCP_URL: https://agentauth.radi.pro/mcp (default)
- AGENTAUTH_ACCESS_TOKEN: access token for AgentAuth MCP tool calls
- SCORE_HALF_LIFE_DAYS: decay half-life (default: 90)
- DISPUTE_PENALTY: per-dispute penalty (default: 0.03)
- ATTESTATION_TTL_HOURS: default validity period (default: 24)
- ATTESTATION_CUMULATIVE_REVOCATION_THRESHOLD: cumulative score drop from issuance to trigger attestation revocation (default: 0.10)
- DISPUTE_FILER_DAILY_CAP: max disputes filed by one agent per 24h (default: 10)
- DISPUTE_FILER_OPEN_CAP: max open disputes one agent may hold at once across all targets (default: 30)
- SYBIL_BURST_24H_THRESHOLD: agents in ±12h window threshold for medium Sybil alert (default: 20)
- SYBIL_BURST_7D_THRESHOLD: agents in ±84h window threshold for slow Sybil alert (default: 50)
- SYBIL_REPORT_VELOCITY_THRESHOLD: distinct negative reports per 24h threshold for velocity signal (default: 50)
- MCP_TRANSPORT: stdio | streamable-http (default: stdio)
- MCP_PORT: port for streamable-http transport (default: 8000)

## Coding Conventions

- All files start with `from __future__ import annotations`
- All IDs are UUIDs, all timestamps are UTC with timezone
- Pydantic v2 for all data validation
- Business logic in engine/ and tools/, models/ is pure ORM
- Auth logic isolated in auth/ behind AuthProvider protocol
- Every tool needs a comprehensive docstring (it's the agent-facing docs)
- Use structlog for all logging
- Type hints on all function signatures
