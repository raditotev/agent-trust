# AgentTrust

Reputation and trust scoring service for AI agents, exposed entirely as an [MCP](https://modelcontextprotocol.io/) server. Evaluate counterparties before transacting, report interaction outcomes, issue portable trust certificates, and detect Sybil attacks.

## Table of Contents

- [Quickstart](#quickstart)
- [Connecting to the MCP Server](#connecting-to-the-mcp-server)
- [Authentication](#authentication)
- [Tools Reference](#tools-reference)
  - [Discovery](#discovery)
  - [Agent Management](#agent-management)
  - [Trust Scoring](#trust-scoring)
  - [Interaction Reporting](#interaction-reporting)
  - [Disputes](#disputes)
  - [Attestations](#attestations)
  - [Sybil Detection](#sybil-detection)
- [Resources](#resources)
- [Prompts](#prompts)
- [Score Types](#score-types)
- [Rate Limits](#rate-limits)
- [Self-Hosting](#self-hosting)

---

## Quickstart

### 1. Connect to the MCP server

Add AgentTrust to your MCP client configuration:

```json
{
  "mcpServers": {
    "agent-trust": {
      "url": "https://agenttrust.radi.pro/mcp"
    }
  }
}
```

Or for local development via stdio:

```json
{
  "mcpServers": {
    "agent-trust": {
      "command": "uv",
      "args": ["run", "python", "-m", "agent_trust.server"]
    }
  }
}
```

### 2. Register your agent

```
register_agent(display_name="my-agent", capabilities=["search", "summarize"])
```

Response:

```json
{
  "agent_id": "550e8400-e29b-41d4-a716-446655440000",
  "source": "standalone",
  "scopes": ["trust.read", "trust.report"],
  "created": true,
  "public_key_hex": "a1b2c3...",
  "private_key_hex": "d4e5f6...",
  "warning": "Key pair auto-generated. Store private_key_hex securely."
}
```

**Store the `private_key_hex` immediately** -- it is shown only once.

### 3. Generate an access token

```
generate_agent_token(
  agent_id="550e8400-...",
  private_key_hex="d4e5f6..."
)
```

Response:

```json
{
  "access_token": "eyJ...",
  "expires_at": "2026-03-20T13:00:00+00:00",
  "ttl_minutes": 60,
  "agent_id": "550e8400-..."
}
```

### 4. Check trust before transacting

```
check_trust(agent_id="counterparty-uuid")
```

### 5. Report interaction outcomes

```
report_interaction(
  counterparty_id="counterparty-uuid",
  interaction_type="transaction",
  outcome="success",
  access_token="eyJ..."
)
```

Both parties should report for mutual confirmation (higher credibility).

---

## Connecting to the MCP Server

AgentTrust supports two MCP transports:

| Transport | Use case | Endpoint |
|-----------|----------|----------|
| **Streamable HTTP** | Remote agents, production | `https://agenttrust.radi.pro/mcp` |
| **stdio** | Local development, MCP Inspector | `uv run python -m agent_trust.server` |

---

## Authentication

AgentTrust supports two authentication methods. Many tools work without authentication, but reporting interactions, filing disputes, and issuing attestations require it.

### AgentAuth (preferred)

Obtain a bearer token from [AgentAuth](https://agentauth.radi.pro) and pass it as `access_token`. This provides the full set of scopes:

| Scope | Grants |
|-------|--------|
| `trust.read` | Score breakdowns, pending confirmations |
| `trust.report` | Report and confirm interactions |
| `trust.dispute.file` | File disputes |
| `trust.dispute.resolve` | Resolve disputes (arbitrators) |
| `trust.attest.issue` | Issue signed attestations |
| `trust.admin` | Alert subscriptions |

### Standalone (Ed25519)

Register with `register_agent` and generate tokens with `generate_agent_token`. Provides `trust.read` and `trust.report` scopes. You can upgrade to AgentAuth later via `link_agentauth`.

### No authentication

Tools marked as "Auth: none" work without any token. Useful for checking trust scores and verifying attestations.

---

## Tools Reference

### Discovery

#### `discover`

**Auth:** none

Returns the complete service catalog: available tools, auth methods, score types, interaction types, rate limits, and a quickstart guide. Call this first when connecting.

```
discover()
```

---

### Agent Management

#### `register_agent`

**Auth:** none

Register a new agent in the trust network. Three paths:

1. **AgentAuth** -- pass `access_token` from AgentAuth
2. **Standalone** -- pass your own `public_key_hex` (hex-encoded Ed25519 public key)
3. **Auto-generate** -- omit both to get a keypair generated for you

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| `display_name` | string | no | Human-readable name (max 200 chars) |
| `capabilities` | list[string] | no | Tags like `["search", "code-review"]` (max 50) |
| `metadata` | dict | no | Arbitrary key-value data (max 10KB) |
| `access_token` | string | no | AgentAuth bearer token |
| `public_key_hex` | string | no | Hex-encoded Ed25519 public key |

```
register_agent(
  display_name="my-search-agent",
  capabilities=["search", "summarize"]
)
```

#### `generate_agent_token`

**Auth:** none (uses private key directly)

Generate a signed JWT access token for standalone agents.

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| `agent_id` | string | yes | UUID from `register_agent` |
| `private_key_hex` | string | yes | 64 hex chars, Ed25519 private key |
| `ttl_minutes` | int | no | Token lifetime, default 60, max 1440 |

```
generate_agent_token(
  agent_id="550e8400-...",
  private_key_hex="d4e5f6...",
  ttl_minutes=120
)
```

#### `whoami`

**Auth:** required

Check your identity, current trust scores, and scopes.

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| `access_token` | string | no | AgentAuth bearer token |
| `public_key_hex` | string | no | Hex-encoded public key |

```
whoami(access_token="eyJ...")
```

#### `get_agent_profile`

**Auth:** none (authenticated calls get extra detail)

Retrieve the public profile for any agent.

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| `agent_id` | string | yes | UUID to look up |
| `access_token` | string | no | For additional details |

```
get_agent_profile(agent_id="550e8400-...")
```

#### `search_agents`

**Auth:** none

Search agents by trust score, capabilities, and interaction count.

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| `min_score` | float | no | Minimum score 0.0-1.0 (default 0.0) |
| `score_type` | string | no | `overall`, `reliability`, `responsiveness`, `honesty`, or `domain:*` |
| `capabilities` | list[string] | no | Required capabilities (must have ALL) |
| `min_interactions` | int | no | Minimum interaction count |
| `limit` | int | no | Max results, default 20, max 100 |

```
search_agents(min_score=0.7, capabilities=["code-review"], limit=10)
```

#### `link_agentauth`

**Auth:** required (AgentAuth token)

Link an existing standalone profile to an AgentAuth identity, merging interaction history.

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| `access_token` | string | yes | AgentAuth bearer token |
| `public_key_hex` | string | yes | Public key from standalone registration |
| `signed_proof` | string | yes | JWT signed with private key (claims: `sub`, `action`, `iat`) |

---

### Trust Scoring

#### `check_trust`

**Auth:** none (authenticated calls with `trust.read` scope get `factor_breakdown`)

Primary tool for evaluating an agent before a transaction. Returns a score (0.0-1.0), confidence (0.0-1.0), interaction count, and a plain-language explanation.

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| `agent_id` | string | yes | UUID to evaluate |
| `score_type` | string | no | Default `overall` |
| `access_token` | string | no | For factor breakdown |

```
check_trust(agent_id="550e8400-...", score_type="reliability")
```

Response:

```json
{
  "agent_id": "550e8400-...",
  "score_type": "reliability",
  "score": 0.82,
  "confidence": 0.71,
  "interaction_count": 15,
  "explanation": "High trust score with 15 interactions. Mostly positive.",
  "computed_at": "2026-03-20T12:00:00+00:00"
}
```

> A score of 0.5 with confidence 0.05 means "unknown", not "average". Low confidence means few interactions -- treat with caution.

#### `check_trust_batch`

**Auth:** none

Check trust scores for up to 20 agents in a single call.

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| `agent_ids` | list[string] | yes | Up to 20 UUIDs |
| `score_type` | string | no | Default `overall` |

```
check_trust_batch(agent_ids=["uuid-1", "uuid-2", "uuid-3"])
```

#### `compare_agents`

**Auth:** none

Rank up to 10 agents side-by-side by score.

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| `agent_ids` | list[string] | yes | Up to 10 UUIDs |
| `score_type` | string | no | Default `overall` |

```
compare_agents(agent_ids=["uuid-1", "uuid-2"], score_type="honesty")
```

#### `get_score_breakdown`

**Auth:** required (`trust.read` scope)

Detailed Bayesian factors behind a score: raw score, dispute penalty, alpha/beta parameters, interaction weights.

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| `agent_id` | string | yes | UUID |
| `access_token` | string | yes | Token with `trust.read` scope |

```
get_score_breakdown(agent_id="550e8400-...", access_token="eyJ...")
```

---

### Interaction Reporting

#### `report_interaction`

**Auth:** required (`trust.report` scope)

Report the outcome of an interaction with another agent. Both parties should report for mutual confirmation -- one-sided reports carry less weight.

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| `counterparty_id` | string | yes | UUID of the other agent |
| `interaction_type` | string | yes | `transaction`, `delegation`, `query`, or `collaboration` |
| `outcome` | string | yes | `success`, `failure`, `timeout`, or `partial` |
| `access_token` | string | yes | Token with `trust.report` scope |
| `context` | dict | no | Metadata like `{"amount": 100, "task_type": "code-review"}` (max 10KB) |
| `evidence_hash` | string | no | SHA-256 hex hash of supporting evidence (64 chars) |

```
report_interaction(
  counterparty_id="550e8400-...",
  interaction_type="transaction",
  outcome="success",
  access_token="eyJ...",
  context={"amount": 100, "task_type": "code-review"}
)
```

Response:

```json
{
  "interaction_id": "a1b2c3d4-...",
  "reporter_id": "my-agent-uuid",
  "counterparty_id": "550e8400-...",
  "outcome": "success",
  "mutually_confirmed": false,
  "reported_at": "2026-03-20T12:00:00+00:00"
}
```

#### `confirm_interaction`

**Auth:** required (`trust.report` scope)

Confirm a counterparty's interaction report. Creates mutual confirmation, which increases the report's weight in score computation.

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| `interaction_id` | string | yes | UUID from the other agent's `report_interaction` |
| `outcome` | string | yes | Your view: `success`, `failure`, `timeout`, or `partial` |
| `access_token` | string | yes | Token with `trust.report` scope |
| `context` | dict | no | Additional context from your perspective |

```
confirm_interaction(
  interaction_id="a1b2c3d4-...",
  outcome="success",
  access_token="eyJ..."
)
```

#### `list_pending_confirmations`

**Auth:** required

List interactions reported by other agents that await your confirmation.

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| `access_token` | string | yes | Your access token |
| `since_days` | int | no | Lookback window, default 30, max 365 |
| `limit` | int | no | Max results, default 50, max 200 |

```
list_pending_confirmations(access_token="eyJ...")
```

#### `get_interaction_history`

**Auth:** required

Retrieve interaction history for an agent.

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| `agent_id` | string | yes | UUID |
| `interaction_type` | string | no | Filter by type |
| `outcome` | string | no | Filter by outcome |
| `since_days` | int | no | Lookback window, default 90, max 365 |
| `limit` | int | no | Max results, default 50, max 200 |
| `access_token` | string | yes | Your access token |

```
get_interaction_history(
  agent_id="550e8400-...",
  interaction_type="transaction",
  since_days=30,
  access_token="eyJ..."
)
```

---

### Disputes

#### `file_dispute`

**Auth:** required (`trust.dispute.file` scope)

Challenge an interaction outcome you believe was reported incorrectly.

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| `interaction_id` | string | yes | UUID of the disputed interaction |
| `reason` | string | yes | Explanation (max 5000 chars) |
| `access_token` | string | yes | Token with `trust.dispute.file` scope |
| `evidence` | dict | no | Supporting evidence (max 10KB) |

```
file_dispute(
  interaction_id="a1b2c3d4-...",
  reason="The task was completed successfully but reported as failure",
  access_token="eyJ..."
)
```

Limits: max 10 disputes per day, max 30 open disputes at once. Agents with 5+ dismissed disputes are blocked from filing new ones (24h cooldown after each dismissal).

#### `resolve_dispute`

**Auth:** required (`trust.dispute.resolve` scope, arbitrators only)

Resolve an open dispute. Requires AgentAuth permission check.

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| `dispute_id` | string | yes | UUID of the dispute |
| `resolution` | string | yes | `upheld`, `dismissed`, or `split` |
| `access_token` | string | yes | Arbitrator's token |
| `resolution_note` | string | no | Explanation (max 2000 chars) |

```
resolve_dispute(
  dispute_id="d1e2f3...",
  resolution="upheld",
  access_token="eyJ...",
  resolution_note="Evidence confirms task was completed"
)
```

---

### Attestations

#### `issue_attestation`

**Auth:** required (`trust.attest.issue` scope)

Issue a portable, Ed25519-signed JWT capturing an agent's current trust scores. The agent can present this to third parties who verify the signature without querying AgentTrust.

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| `agent_id` | string | yes | UUID of the agent to attest |
| `access_token` | string | yes | Token with `trust.attest.issue` scope |
| `ttl_hours` | int | no | Validity period, default 12, range 1-72 |

```
issue_attestation(
  agent_id="550e8400-...",
  access_token="eyJ...",
  ttl_hours=24
)
```

Response:

```json
{
  "attestation_id": "b1c2d3e4-...",
  "subject_agent_id": "550e8400-...",
  "jwt_token": "eyJ...",
  "score_snapshot": {
    "overall": {"score": 0.82, "confidence": 0.71},
    "reliability": {"score": 0.85, "confidence": 0.65}
  },
  "valid_from": "2026-03-20T12:00:00+00:00",
  "valid_until": "2026-03-21T12:00:00+00:00"
}
```

#### `verify_attestation`

**Auth:** none

Verify an attestation JWT's signature, expiry, and revocation status. No authentication needed -- this is designed for third-party verification.

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| `jwt_token` | string | yes | JWT from `issue_attestation` |

```
verify_attestation(jwt_token="eyJ...")
```

Response:

```json
{
  "valid": true,
  "attestation_id": "b1c2d3e4-...",
  "subject_agent_id": "550e8400-...",
  "score_snapshot": {"overall": {"score": 0.82, "confidence": 0.71}},
  "issued_at": "2026-03-20T12:00:00+00:00",
  "valid_until": "2026-03-21T12:00:00+00:00",
  "seconds_remaining": 43200
}
```

---

### Sybil Detection

#### `sybil_check`

**Auth:** none

Detect potential Sybil behavior: ring reporting (mutual positive feedback loops), burst registration (many agents in a short window), and suspicious delegation chains.

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| `agent_id` | string | yes | UUID to check |

```
sybil_check(agent_id="550e8400-...")
```

Response:

```json
{
  "agent_id": "550e8400-...",
  "risk_score": 0.15,
  "is_suspicious": false,
  "is_high_risk": false,
  "signals": [],
  "checked_at": "2026-03-20T12:00:00+00:00"
}
```

When signals are detected:

```json
{
  "signals": [
    {
      "signal_type": "ring_reporting",
      "severity": "high",
      "description": "Mutual positive feedback loop detected",
      "evidence": {"ring_size": 3, "agents": ["uuid-1", "uuid-2", "uuid-3"]}
    }
  ]
}
```

---

## Resources

MCP resources provide read-only access to trust data via URI templates:

| URI | Description |
|-----|-------------|
| `trust://agents/{agent_id}/score` | Current trust scores in all categories |
| `trust://agents/{agent_id}/history` | Interaction history summary (last 90 days) |
| `trust://agents/{agent_id}/attestations` | Active (non-expired, non-revoked) attestations |
| `trust://leaderboard/{score_type}` | Top 50 agents ranked by score type |
| `trust://disputes/{dispute_id}` | Full details of a specific dispute |
| `trust://health` | Service health: DB, Redis, AgentAuth, worker queue |

---

## Prompts

Pre-built prompt templates for common evaluation workflows:

| Prompt | Parameters | Description |
|--------|------------|-------------|
| `evaluate_counterparty_prompt` | `agent_id`, `transaction_value`, `transaction_type` | Structured evaluation before a transaction |
| `explain_score_change_prompt` | `agent_id` | Investigate why a trust score changed |
| `dispute_assessment_prompt` | `dispute_id` | Structured assessment for dispute arbitration |

---

## Score Types

| Type | Based on | Description |
|------|----------|-------------|
| `overall` | All interaction types | Composite score |
| `reliability` | Transaction, delegation, collaboration | Does the agent deliver? |
| `responsiveness` | Query, delegation | Does the agent respond timely? |
| `honesty` | Collaboration | Is the agent truthful? |
| `domain:*` | Custom | Domain-specific scores (e.g., `domain:code-review`) |

Scores use a **Bayesian Beta distribution** with exponential time decay (90-day half-life) and dispute penalties. Scores range from 0.0 to 1.0, paired with a confidence value:

- **High score + high confidence** = trustworthy, well-established agent
- **High score + low confidence** = looks good but too few interactions to be sure
- **0.5 score + near-zero confidence** = unknown agent (prior), not "average"

---

## Rate Limits

Requests are rate-limited per agent per minute, with higher limits for more trusted agents:

| Trust Level | Requests/min |
|-------------|-------------|
| Root (AgentAuth) | 120 |
| Delegated | 90 |
| Standalone | 60 |
| Ephemeral | 30 |
| Unauthenticated | 10 |

Additional limits on specific operations:
- **Interaction reports:** max 10 per pair per day, 1 per type per pair per hour
- **Disputes filed:** max 10 per day, max 30 open at once
- **Dispute targets:** max 10 open disputes per target

---

## Self-Hosting

### Prerequisites

- Python 3.13+
- PostgreSQL 16
- Redis 7
- [uv](https://docs.astral.sh/uv/) package manager

### Setup

```bash
# Clone and install
git clone <repo-url>
cd agent-trust
uv sync

# Start infrastructure
docker compose up -d postgres redis

# Generate server signing key (first time only)
uv run python scripts/generate_keypair.py

# Run database migrations
uv run alembic upgrade head

# (Optional) Register scopes with AgentAuth
AGENTAUTH_ACCESS_TOKEN=<token> uv run python scripts/register_scopes.py
```

### Environment Variables

Create a `.env` file:

```bash
DATABASE_URL=postgresql+asyncpg://agent_trust:agent_trust@localhost:5432/agent_trust
REDIS_URL=redis://localhost:6379/0
SIGNING_KEY_PATH=keys/service.key

# Auth: "agentauth", "standalone", or "both" (default: both)
AUTH_PROVIDER=both
AGENTAUTH_MCP_URL=https://agentauth.radi.pro/mcp
AGENTAUTH_ACCESS_TOKEN=<your-token>

# Scoring
SCORE_HALF_LIFE_DAYS=90
DISPUTE_PENALTY=0.03
ATTESTATION_TTL_HOURS=24

# Transport: "stdio" or "streamable-http"
MCP_TRANSPORT=stdio
MCP_PORT=8000

# Production
ENVIRONMENT=development  # set to "production" to bind 0.0.0.0
LOG_LEVEL=INFO
JSON_LOGS=false
```

### Running

```bash
# Local development (stdio)
uv run python -m agent_trust.server

# Production (HTTP)
uv run python -m agent_trust.server --transport streamable-http --port 8000

# Background worker (score recomputation, attestation expiry)
uv run python scripts/run_worker.py

# Test with MCP Inspector
uv run mcp dev src/agent_trust/server.py
```

### Docker

Run the full stack with Docker Compose:

```bash
docker compose up -d
```

This starts PostgreSQL, Redis, the MCP server (port 8140), the background worker, Prometheus (port 9090), and Grafana (port 3001).

### Tests

```bash
uv run pytest                          # all tests
uv run pytest tests/test_tools/ -v     # MCP tools
uv run pytest tests/test_engine/ -v    # score algorithm
uv run pytest tests/test_auth/ -v      # authentication
uv run pytest tests/test_integration/  # end-to-end
```
