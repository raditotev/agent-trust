# AgentTrust

**AgentTrust** is a pure MCP-only reputation and trust scoring server for AI agents. It provides cryptographically-signed trust scores, interaction history, dispute resolution, and issuance attestation all via the Model Context Protocol. No REST API.

## Quick Start

```bash
cp .env.example .env
uv sync
docker compose up -d postgres redis
uv run alembic upgrade head
uv run python scripts/generate_keypair.py   # first time only
uv run python scripts/register_scopes.py   # register trust.* scopes in AgentAuth

# Run server (stdio — for local testing with MCP Inspector)
uv run python -m agent_trust.server

# Run server (Streamable HTTP — for remote agents)
uv run python -m agent_trust.server --transport streamable-http --port 8000

# Run background workers (score recomputation, decay, expiry)
uv run python scripts/run_worker.py

# Test with MCP Inspector
uv run mcp dev src/agent_trust/server.py
```

## Architecture

AgentTrust is a **pure MCP server** — all functionality is exposed as MCP tools, resources, and prompts. There are no HTTP endpoints beyond the MCP transport itself.

```
AI Agent
   │  (MCP client)
   ▼
AgentTrust MCP Server
   ├── Tools (actions)
   ├── Resources (read-only data)
   └── Prompts (LLM reasoning templates)
   │
   ├── PostgreSQL + TimescaleDB  (interactions, scores, disputes, attestations)
   ├── Redis                     (score cache, introspection cache, rate limits)
   ├── arq workers               (score recomputation, decay refresh, expiry)
   └── AgentAuth MCP             (token introspection, RBAC, trust levels)
        (agentauth.radi.pro/mcp)
```

**Key design decisions:**

- All interactions are immutable append-only events
- Scores are recomputed asynchronously via arq workers
- AgentTrust connects to AgentAuth as an MCP _client_ — dogfooding MCP-first philosophy
- Token introspection results cached in Redis (SHA-256 key, TTL = min(token_expiry, 300s))
- Attestations are portable Ed25519-signed JWTs

## MCP Tools

### Agent Management

| Tool                | Scope Required | Description                                  |
| ------------------- | -------------- | -------------------------------------------- |
| `register_agent`    | none           | Register a new agent and get an agent_id     |
| `link_agentauth`    | none           | Link an AgentAuth token to an existing agent |
| `whoami`            | none           | Get your own agent identity                  |
| `get_agent_profile` | none           | Get public profile for any agent             |
| `search_agents`     | none           | Search agents by name or description         |

### Trust Scoring

| Tool                  | Scope Required       | Description                         |
| --------------------- | -------------------- | ----------------------------------- |
| `check_trust`         | none (optional auth) | Get trust score for an agent        |
| `get_score_breakdown` | trust.read           | Detailed score factor breakdown     |
| `compare_agents`      | none                 | Rank up to 10 agents by trust score |

### Interactions

| Tool                      | Scope Required | Description                          |
| ------------------------- | -------------- | ------------------------------------ |
| `report_interaction`      | trust.report   | Submit an interaction report         |
| `get_interaction_history` | trust.read     | Get interaction history for an agent |

### Disputes

| Tool              | Scope Required                         | Description                          |
| ----------------- | -------------------------------------- | ------------------------------------ |
| `file_dispute`    | trust.dispute.file                     | File a dispute about an interaction  |
| `resolve_dispute` | trust.dispute.resolve + AgentAuth RBAC | Resolve a dispute (arbitrators only) |

### Attestations

| Tool                 | Scope Required     | Description                      |
| -------------------- | ------------------ | -------------------------------- |
| `issue_attestation`  | trust.attest.issue | Issue a signed trust attestation |
| `verify_attestation` | none               | Verify an attestation JWT        |

### Safety & Admin

| Tool               | Scope Required | Description                                                                       |
| ------------------ | -------------- | --------------------------------------------------------------------------------- |
| `sybil_check`      | none           | Run sybil detection checks (ring reporting, burst registration, delegation chain) |
| `subscribe_alerts` | trust.admin    | Subscribe to score change notifications                                           |

## MCP Resources

| URI                                | Description                       |
| ---------------------------------- | --------------------------------- |
| `trust://agents/{id}/score`        | Current trust scores for an agent |
| `trust://agents/{id}/history`      | Interaction history               |
| `trust://agents/{id}/attestations` | Active attestations               |
| `trust://leaderboard/{score_type}` | Top agents by score type          |
| `trust://disputes/{id}`            | Dispute details                   |
| `trust://health`                   | Server health and connectivity    |

## MCP Prompts

| Prompt                  | Description                                               |
| ----------------------- | --------------------------------------------------------- |
| `evaluate_counterparty` | Systematic PROCEED/CAUTION/DECLINE trust assessment       |
| `explain_score_change`  | Diagnostic investigation of score changes                 |
| `dispute_assessment`    | Structured arbitrator guide for evidence-based resolution |

## Score Algorithm

AgentTrust uses a **Bayesian Beta distribution** model:

- **Prior**: α=2, β=2 → new agents start at 0.5 with low confidence
- **Time decay**: `weight = 0.5 ^ (age_days / half_life_days)` (default half-life: 90 days)
- **Reporter credibility**: `(0.5 + reporter_trust × 0.5) × trust_level_weight × sybil_multiplier × interaction_count_penalty`
- **Trust level weights**: root=1.2×, delegated=1.0×, standalone=0.8×, ephemeral=0.7× — derived from `auth_source`/`agentauth_linked`, never from user-supplied metadata
- **Sybil multiplier**: 0.3× (high risk), 0.6× (suspicious), 1.0× (clean) — via `SybilDetector`
- **New-reporter gate**: `interaction_count_penalty = 0.3` for reporters with < 3 recorded interactions
- **Mutual confirmation bonus**: `max(1.5 - 0.1 × (pair_count - 1), 1.0)` — diminishing returns per pair (1st: 1.5×, 6th+: 1.0×)
- **Upheld dispute penalty**: `max(1.0 - n_upheld × 0.03, 0.50)` (floor: 0.50)
- **Dismissed dispute penalty** (filer): exponential `max(1.0 - Σ(0.01 × 1.5ⁱ), 0.90)` — floor reached at ~5–6 dismissals
- **Confidence**: `1.0 - 1.0 / (1.0 + n × 0.1)`

**For a detailed walkthrough including role-aware scoring, Sybil detection, and worked examples, see [docs/interaction-scoring.md](docs/interaction-scoring.md).**

## Environment Variables

| Variable                 | Default                          | Description                                             |
| ------------------------ | -------------------------------- | ------------------------------------------------------- |
| `DATABASE_URL`           | —                                | PostgreSQL connection string (`postgres+asyncpg://...`) |
| `REDIS_URL`              | `redis://localhost:6379/0`       | Redis connection string                                 |
| `SIGNING_KEY_PATH`       | `keys/signing_key.pem`           | Path to Ed25519 private key                             |
| `AUTH_PROVIDER`          | `both`                           | `agentauth` \| `standalone` \| `both`                   |
| `AGENTAUTH_MCP_URL`      | `https://agentauth.radi.pro/mcp` | AgentAuth MCP endpoint                                  |
| `AGENTAUTH_ACCESS_TOKEN` | —                                | Bearer token for AgentAuth MCP calls                    |
| `SCORE_HALF_LIFE_DAYS`   | `90`                             | Score decay half-life in days                           |
| `DISPUTE_PENALTY`        | `0.03`                           | Per-upheld-dispute score penalty                        |
| `ATTESTATION_TTL_HOURS`  | `12`                             | Default attestation validity period                     |
| `MCP_TRANSPORT`          | `stdio`                          | `stdio` \| `streamable-http`                            |
| `MCP_PORT`               | `8000`                           | Port for streamable-http transport                      |
| `LOG_LEVEL`              | `INFO`                           | Logging level                                           |
| `JSON_LOGS`              | `false`                          | JSON log format (set `true` in production)              |

## Rate Limits

Per-agent sliding window (60 seconds):

| Trust Level     | Requests/min |
| --------------- | ------------ |
| `root`          | 300          |
| `delegated`     | 120          |
| `standalone`    | 60           |
| `ephemeral`     | 30           |
| Unauthenticated | 10           |

## Testing

```bash
# Run the full test suite
uv run pytest

# Quick summary (no verbose output)
uv run pytest --tb=short -q

# Run a specific test suite
uv run pytest tests/test_engine/ -v        # score algorithm (Bayesian model, decay, penalties)
uv run pytest tests/test_auth/ -v          # auth layer (AgentAuth, standalone Ed25519)
uv run pytest tests/test_tools/ -v         # MCP tool unit tests (all 15 tools)
uv run pytest tests/test_integration/ -v   # MCP protocol-level integration tests

# MCP protocol integration tests only (68 tests, no DB/Redis required)
uv run pytest tests/test_integration/test_mcp_protocol.py -v

# Run a single test class or test
uv run pytest tests/test_integration/test_mcp_protocol.py::TestRegisterAgentMCP -v
uv run pytest tests/test_tools/test_agent_tools.py::test_register_agent_standalone -v

# Show coverage
uv run pytest --cov=agent_trust --cov-report=term-missing
```

The MCP protocol integration tests (`test_mcp_protocol.py`) use an in-process MCP client/server pair — no running database or Redis instance is required. All external dependencies are mocked per-test.

## Development

```bash
# Install dependencies
uv sync --extra dev

# Lint and format
uv run ruff check src/
uv run ruff format src/

# Create a new migration
uv run alembic revision --autogenerate -m "description"

# Seed test agents
uv run python scripts/seed_test_agents.py
```

## Deployment

```bash
# Build image
docker build -t agent-trust:latest .

# Full stack with docker compose
docker compose up -d

# Run migrations on first deploy
docker compose exec agent-trust uv run alembic upgrade head

# Generate signing keypair
docker compose exec agent-trust uv run python scripts/generate_keypair.py
```
