# AgentTrust — MCP Server for Reputation & Trust Scoring

## Claude Code Project Plan

---

## Vision

A pure MCP server that AI agents connect to as a tool. No REST API, no dashboard, no human-facing UI. Agents call tools like `check_trust`, `report_interaction`, `file_dispute`, and `get_attestation` through the Model Context Protocol. The server is the credit bureau of the agent economy — agents query it before entering transactions, report outcomes after, and build portable reputation over time.

**Core principle:** Agents are the only principals. There are no human users, no admin panels, no OAuth consent screens. An agent connects, authenticates via AgentAuth (or standalone key for bootstrap), and calls tools.

**Identity integration:** AgentAuth (`agentauth.radi.pro`) is the identity layer. Agents that already have an AgentAuth identity can use AgentTrust immediately — no separate registration. AgentTrust connects to AgentAuth's MCP server (`agentauth.radi.pro/mcp`) as an MCP client and calls tools like `introspect_token` and `check_permission` to verify agent identity, trust level, scopes, and delegation chains. Standalone Ed25519 keys are supported as a fallback for agents not yet on AgentAuth.

---

## Architecture

```
┌──────────────────────────────────────────────────┐
│              MCP Client (Any Agent)              │
│    Claude, LangChain, CrewAI, AutoGen, etc.      │
└──────────────────┬───────────────────────────────┘
                   │ Streamable HTTP / stdio
                   │
┌──────────────────▼───────────────────────────────┐
│            AgentTrust MCP Server                 │
│          (FastMCP + Starlette)                   │
│                                                  │
│  ┌─────────────────────────────────────────────┐ │
│  │         Auth Middleware                     │ │
│  │  AgentAuth token → introspect → identity    │ │
│  │  Standalone key → verify → identity         │ │
│  │  (configurable: agentauth | standalone)     │ │
│  └─────────────────────────────────────────────┘ │
│                                                  │
│  Tools:                                          │
│    register_agent       check_trust              │
│    report_interaction   get_score_breakdown      │
│    file_dispute         resolve_dispute          │
│    issue_attestation    verify_attestation       │
│    compare_agents       get_interaction_history  │
│    subscribe_alerts     search_agents            │
│    link_agentauth       whoami                   │
│                                                  │
│  Resources:                                      │
│    trust://agents/{id}/score                     │
│    trust://agents/{id}/history                   │
│    trust://agents/{id}/attestations              │
│    trust://leaderboard/{score_type}              │
│    trust://disputes/{id}                         │
│    trust://health                                │
│                                                  │
│  Prompts:                                        │
│    evaluate_counterparty                         │
│    explain_score_change                          │
│    dispute_assessment                            │
│                                                  │
└───────┬──────────┬───────────────┬───────────────┘
        │          │               │
   ┌────▼─────┐ ┌──▼──────┐  ┌─────▼──────┐
   │PostgreSQL│ │  Redis  │  │ Background │
   │+Timescale│ │ Cache + │  │  Workers   │
   │          │ │ Streams │  │  (arq)     │
   └──────────┘ └─────────┘  └────────────┘
        │
        │  ┌──────────────────────────────┐
        └──┤  AgentAuth MCP Server        │
           │  agentauth.radi.pro/mcp      │
           │  (MCP client → MCP server)   │
           │  Tools: introspect_token,    │
           │    check_permission,         │
           │    quickstart, authenticate  │
           └──────────────────────────────┘
```

**How AgentAuth fits in:**

1. Agent connects to AgentTrust MCP server
2. Passes its AgentAuth `access_token` as a tool parameter
3. AgentTrust acts as an MCP client to AgentAuth's MCP server (`agentauth.radi.pro/mcp`)
4. Calls AgentAuth's `introspect_token` MCP tool to verify the token
5. If valid → extracts `agent_id`, `agent_type`, `trust_level`, `scopes`
6. If the agent_id doesn't exist in AgentTrust's DB, auto-creates a profile (zero-friction onboarding)
7. Tool executes with the verified identity context
8. For permission-gated tools (e.g., `resolve_dispute`), AgentTrust calls AgentAuth's `check_permission` MCP tool

**Why dual auth (AgentAuth + standalone):**

- AgentAuth is the preferred path — agents already in the ecosystem get instant access
- Standalone Ed25519 is the bootstrap path — agents not yet on AgentAuth can still participate
- Config switch: `AUTH_PROVIDER=agentauth` (default) or `AUTH_PROVIDER=standalone` or `AUTH_PROVIDER=both`

---

## Technology Stack

| Layer           | Technology                                   | Rationale                                                                          |
| --------------- | -------------------------------------------- | ---------------------------------------------------------------------------------- |
| MCP Framework   | `mcp` SDK (FastMCP)                          | Official Python SDK; decorator-based tool registration, auto schema generation     |
| Runtime         | Python 3.12+                                 | FastMCP's native language, best ecosystem support                                  |
| Package Manager | `uv`                                         | Fast, deterministic dependency resolution                                          |
| Transport       | Streamable HTTP (primary), stdio (local dev) | HTTP for production remote access, stdio for testing                               |
| ASGI Host       | Starlette (via FastMCP) + Uvicorn            | FastMCP mounts directly onto Starlette; production-grade                           |
| Database        | PostgreSQL 16 + TimescaleDB                  | Time-series hypertables for interaction events, relational for everything else     |
| Cache           | Redis 7                                      | Score caching, rate limiting, pub/sub for alert dispatch                           |
| Background Jobs | arq (Redis-backed)                           | Lightweight async workers for score recomputation, attestation expiry              |
| Identity        | AgentAuth MCP server + Ed25519 fallback      | AgentTrust connects as MCP client to AgentAuth's MCP server; PyNaCl for standalone |
| Attestations    | PyJWT + Ed25519 service key                  | Sign attestations with service key; AgentAuth identity embedded in JWT claims      |
| Testing         | pytest + hypothesis + mcp client SDK         | Property-based scoring tests + MCP protocol-level integration tests                |
| Migrations      | Alembic                                      | Schema versioning                                                                  |
| Linting         | ruff                                         | Fast, comprehensive                                                                |

---

## Identity & Authentication Model

### AgentAuth Integration (Primary)

```
Agent                    AgentTrust                    AgentAuth
  │                          │                             │
  │  call check_trust(       │                             │
  │    access_token="eyJ.."  │                             │
  │    agent_id="xyz")       │                             │
  │─────────────────────────>│                             │
  │                          │  introspect_token("eyJ..") │
  │                          │────────────────────────────>│
  │                          │  {active: true,             │
  │                          │   sub: "agent-uuid",        │
  │                          │   scopes: [...],            │
  │                          │   trust_level: "root"}      │
  │                          │<────────────────────────────│
  │                          │                             │
  │                          │  (auto-create profile if    │
  │                          │   agent_id not in local DB) │
  │                          │                             │
  │  {score: 0.82,           │                             │
  │   confidence: 0.91}      │                             │
  │<─────────────────────────│                             │
```

**AgentAuth scopes used by AgentTrust:**

Custom scopes registered in AgentAuth for trust-service operations:

- `trust.read` — check scores, view profiles, read history
- `trust.report` — report interactions
- `trust.dispute.file` — file disputes
- `trust.dispute.resolve` — resolve disputes (requires arbitrator role)
- `trust.attest.issue` — request attestations
- `trust.admin` — manage alert subscriptions, bulk operations

These scopes are registered in AgentAuth via its MCP tools. When an agent gets a credential scoped to `trust.report`, it can report interactions but not resolve disputes.

### Standalone Fallback (Bootstrap)

For agents not on AgentAuth:

- Agent calls `register_agent` with `public_key_hex` (Ed25519)
- AgentTrust stores the key and issues a local session token
- All subsequent tool calls include this token
- Standalone agents get limited default scopes: `trust.read` + `trust.report`
- Agent can later call `link_agentauth` to associate their profile with an AgentAuth identity and unlock full scopes

### Token Introspection Caching

To avoid hammering AgentAuth's MCP server on every tool call:

- AgentTrust caches introspection results in Redis (keyed by token hash)
- Cache TTL = min(token_expiry, 5 minutes) — never cache longer than the token lives
- On cache hit: skip introspection, use cached identity
- On token revocation: AgentAuth's blocklist handles it; our short cache TTL limits exposure

```python
async def cached_introspect(access_token: str) -> dict:
    cache_key = f"introspect:{sha256(access_token)}"
    cached = await redis.get(cache_key)
    if cached:
        return json.loads(cached)

    # Call AgentAuth's introspect_token MCP tool
    result = await agentauth_mcp_client.call_tool(
        "introspect_token", {"token": access_token}
    )
    if result["active"]:
        ttl = min(result["exp"] - time.time(), 300)  # max 5 min
        await redis.setex(cache_key, int(ttl), json.dumps(result))
    return result
```

### Identity Resolution Flow

```python
async def resolve_identity(
    access_token: str | None,
    public_key_hex: str | None,
) -> AgentIdentity:
    """Resolve agent identity from either AgentAuth token or standalone key."""
    if access_token:
        introspection = await cached_introspect(access_token)
        if introspection["active"]:
            agent_id = introspection["sub"]
            await ensure_local_profile(
                agent_id=agent_id,
                agent_type=introspection.get("agent_type"),
                trust_level=introspection.get("trust_level"),
                source="agentauth",
            )
            return AgentIdentity(
                agent_id=agent_id,
                source="agentauth",
                scopes=introspection.get("scopes", []),
                trust_level=introspection.get("trust_level"),
            )
        raise AuthenticationError("Invalid or expired AgentAuth token")

    if public_key_hex:
        agent = await lookup_by_public_key(public_key_hex)
        if agent:
            return AgentIdentity(
                agent_id=str(agent.agent_id),
                source="standalone",
                scopes=["trust.read", "trust.report"],
                trust_level="ephemeral",
            )
        raise AuthenticationError("Unknown public key — register first")

    raise AuthenticationError(
        "Provide access_token (AgentAuth) or public_key_hex (standalone)"
    )
```

---

## MCP Tool Definitions

### Agent Registration & Identity

```python
@mcp.tool()
async def register_agent(
    display_name: str | None = None,
    capabilities: list[str] | None = None,
    metadata: dict | None = None,
    access_token: str | None = None,
    public_key_hex: str | None = None,
) -> dict:
    """Register a new agent in the trust network.

    Two registration paths:
    1. AgentAuth (preferred): Provide your AgentAuth access_token.
       Your identity is verified via token introspection and your
       AgentAuth agent_id becomes your trust profile ID. Zero config.
    2. Standalone: Provide an Ed25519 public_key_hex. You get a
       local trust profile with limited scopes (trust.read + trust.report).
       You can link to AgentAuth later via the link_agentauth tool.

    Returns agent_id, registration source, and trust profile summary."""

@mcp.tool()
async def link_agentauth(
    access_token: str,
    public_key_hex: str,
) -> dict:
    """Link a standalone trust profile to an AgentAuth identity.
    Provide your AgentAuth access_token and the public_key_hex you
    originally registered with. Your interaction history and scores
    transfer to the AgentAuth identity. The standalone profile is
    merged and deactivated. This is a one-time, irreversible operation."""

@mcp.tool()
async def whoami(
    access_token: str | None = None,
    public_key_hex: str | None = None,
) -> dict:
    """Check your identity as AgentTrust sees it. Returns your
    agent_id, registration source (agentauth or standalone),
    trust scores summary, interaction count, active scopes,
    and registration date. Useful for verifying your auth is
    working correctly before making other calls."""

@mcp.tool()
async def get_agent_profile(
    agent_id: str,
    access_token: str | None = None,
) -> dict:
    """Retrieve an agent's public profile including registration date,
    capabilities, trust summary, and interaction count. Authentication
    is optional — unauthenticated calls get a summary view,
    authenticated calls get full detail including AgentAuth metadata.
    Use this to evaluate a potential counterparty before transacting."""

@mcp.tool()
async def search_agents(
    min_score: float = 0.0,
    score_type: str = "overall",
    capabilities: list[str] | None = None,
    min_interactions: int = 0,
    limit: int = 20,
    access_token: str | None = None,
) -> dict:
    """Search for agents meeting trust criteria. Filter by minimum
    score, required capabilities, and minimum interaction count.
    Returns matching agents ranked by score. Use this to find
    trustworthy agents for a specific task type."""
```

### Trust Scoring (the hot path)

```python
@mcp.tool()
async def check_trust(
    agent_id: str,
    score_type: str = "overall",
    access_token: str | None = None,
) -> dict:
    """Check an agent's trust score before entering a transaction.
    Returns score (0.0-1.0), confidence (0.0-1.0), interaction_count,
    and score_age_seconds. Score types: overall, reliability,
    responsiveness, honesty, or domain:{name} for domain-specific
    scores. Low confidence means the agent has few interactions —
    treat with caution regardless of score value.

    Authentication optional for basic queries. Authenticated requests
    (via access_token) get richer data including factor breakdown."""

@mcp.tool()
async def get_score_breakdown(
    agent_id: str,
    access_token: str,
) -> dict:
    """Get a detailed breakdown of how an agent's trust score was
    computed. Returns factor attribution: what percentage came from
    successful interactions, time decay impact, credibility weighting
    of reporters, and dispute penalties. Use this to understand WHY
    an agent has a particular score, not just the number.
    Requires authentication (access_token from AgentAuth)."""

@mcp.tool()
async def compare_agents(
    agent_ids: list[str],
    score_type: str = "overall",
    access_token: str | None = None,
) -> dict:
    """Compare trust scores of multiple agents side by side. Returns
    ranked list with scores, confidence levels, and interaction counts.
    Useful when choosing between multiple agents for a task. Maximum
    10 agents per comparison."""
```

### Interaction Reporting

```python
@mcp.tool()
async def report_interaction(
    counterparty_id: str,
    interaction_type: str,
    outcome: str,
    access_token: str,
    context: dict | None = None,
    evidence_hash: str | None = None,
) -> dict:
    """Report the outcome of an interaction with another agent.
    REQUIRES authentication — your identity is recorded as the reporter.
    Both parties should report for maximum credibility — one-sided
    reports carry less weight in score computation.

    interaction_type: transaction | delegation | query | collaboration
    outcome: success | failure | timeout | partial
    context: optional dict with amount, task_type, duration_ms, sla_met
    evidence_hash: optional SHA-256 hash of supporting evidence

    Returns interaction_id and whether the counterparty has also
    reported on this interaction."""

@mcp.tool()
async def get_interaction_history(
    agent_id: str,
    interaction_type: str | None = None,
    outcome: str | None = None,
    since_days: int = 90,
    limit: int = 50,
    access_token: str | None = None,
) -> dict:
    """Retrieve interaction history for an agent. Filter by type
    and outcome. Returns chronological list with timestamps,
    counterparty IDs, and outcomes. Useful for due diligence
    before high-value transactions."""
```

### Attestations (portable trust)

```python
@mcp.tool()
async def issue_attestation(
    agent_id: str,
    access_token: str,
    ttl_hours: int = 24,
) -> dict:
    """Issue a signed attestation (JWT) capturing an agent's current
    trust scores. The attestation is portable — the agent can present
    it to third parties who verify the signature without querying
    this service.

    The JWT includes the agent's AgentAuth identity (if linked),
    so verifiers can cross-reference with AgentAuth's JWKS endpoint.

    Returns the JWT token, score snapshot, and expiry timestamp.
    Requires authentication via access_token."""

@mcp.tool()
async def verify_attestation(jwt_token: str) -> dict:
    """Verify an attestation's signature, check expiry, and confirm
    it hasn't been revoked. Returns validity status, the embedded
    score snapshot, subject agent_id, and time remaining until expiry.

    No authentication required — anyone can verify an attestation.
    This is by design: attestations are meant to be portable."""
```

### Disputes

```python
@mcp.tool()
async def file_dispute(
    interaction_id: str,
    reason: str,
    access_token: str,
    evidence: dict | None = None,
) -> dict:
    """File a dispute against an interaction outcome. Provide the
    interaction_id from a previously reported interaction and a
    clear reason. REQUIRES authentication.

    The dispute enters 'open' status and will be reviewed by an
    arbitrator agent. Filing frivolous disputes damages your own
    trust score. Returns dispute_id and status."""

@mcp.tool()
async def resolve_dispute(
    dispute_id: str,
    resolution: str,
    access_token: str,
    resolution_note: str | None = None,
) -> dict:
    """Resolve an open dispute. REQUIRES arbitrator authorization.

    The caller's access_token is checked against AgentAuth:
    - Token is introspected to verify identity
    - AgentAuth check_permission is called to verify the agent
      has 'execute' action on '/trust/disputes/resolve' resource
    - Agent must have 'trust.dispute.resolve' scope

    resolution: upheld | dismissed | split
    An upheld dispute penalizes the agent it was filed against.
    A dismissed dispute slightly penalizes the filer.
    Returns updated dispute status and affected agent scores."""
```

### Alerts

```python
@mcp.tool()
async def subscribe_alerts(
    watched_agent_id: str,
    callback_tool: str,
    access_token: str,
    threshold_delta: float = 0.05,
) -> dict:
    """Subscribe to trust score change notifications for an agent.
    When the watched agent's score changes by more than threshold_delta,
    a notification is dispatched. callback_tool is the MCP tool name
    on YOUR server that should be called with the alert payload.
    Requires authentication. Returns subscription_id."""
```

---

## MCP Resources

```python
@mcp.resource("trust://agents/{agent_id}/score")
async def agent_score_resource(agent_id: str) -> str:
    """Current trust scores for an agent in all categories."""

@mcp.resource("trust://agents/{agent_id}/history")
async def agent_history_resource(agent_id: str) -> str:
    """Recent interaction history summary (last 90 days)."""

@mcp.resource("trust://agents/{agent_id}/attestations")
async def agent_attestations_resource(agent_id: str) -> str:
    """Active (non-expired, non-revoked) attestations for an agent."""

@mcp.resource("trust://leaderboard/{score_type}")
async def leaderboard_resource(score_type: str) -> str:
    """Top 50 agents ranked by the specified score type."""

@mcp.resource("trust://disputes/{dispute_id}")
async def dispute_resource(dispute_id: str) -> str:
    """Full details of a specific dispute."""

@mcp.resource("trust://health")
async def health_resource() -> str:
    """Service health: DB, Redis, AgentAuth reachability, worker queue."""
```

---

## MCP Prompts

```python
@mcp.prompt()
def evaluate_counterparty(
    agent_id: str,
    transaction_value: str = "unknown",
    transaction_type: str = "general",
) -> str:
    """Structured evaluation of a potential counterparty."""
    return f"""Evaluate agent {agent_id} as a potential counterparty for
a {transaction_type} transaction (value: {transaction_value}).

Steps:
1. Call check_trust for agent {agent_id} with score_type "overall"
2. If score < 0.3 → DECLINE. If confidence < 0.2 → flag as UNVERIFIED.
3. Call get_score_breakdown to understand score composition.
4. Call get_interaction_history to review recent track record.
5. Check for open disputes.
6. Synthesize findings into: PROCEED / CAUTION / DECLINE with reasoning.

Weight confidence heavily — a 0.8 score with 0.1 confidence is riskier
than a 0.6 score with 0.9 confidence."""

@mcp.prompt()
def explain_score_change(agent_id: str) -> str:
    """Diagnostic prompt for understanding a trust score change."""
    return f"""Investigate the trust score change for agent {agent_id}.

Steps:
1. Call check_trust to get current score and confidence.
2. Call get_score_breakdown for factor attribution.
3. Call get_interaction_history with since_days=7 for recent activity.
4. Check for recently resolved disputes.
5. Identify the primary driver: new interactions, dispute resolution,
   time decay, or credibility reweighting.
6. Summarize what happened and whether the change is concerning."""
```

---

## Data Model

```sql
-- Agent profiles (linked to AgentAuth or standalone)
CREATE TABLE agents (
    agent_id        UUID PRIMARY KEY,                -- matches AgentAuth agent_id when linked
    display_name    TEXT,
    capabilities    TEXT[] DEFAULT '{}',
    metadata        JSONB DEFAULT '{}',
    trust_level     NUMERIC(5,4) DEFAULT 0.5000,     -- cached overall score

    -- Identity source
    auth_source     TEXT NOT NULL DEFAULT 'agentauth', -- 'agentauth' | 'standalone'
    public_key      BYTEA,                            -- only for standalone agents
    agentauth_linked BOOLEAN DEFAULT false,           -- true if verified via AgentAuth

    registered_at   TIMESTAMPTZ DEFAULT now(),
    status          TEXT DEFAULT 'active'
);
CREATE INDEX idx_agents_public_key ON agents(public_key) WHERE public_key IS NOT NULL;
CREATE INDEX idx_agents_capabilities ON agents USING GIN(capabilities);
CREATE INDEX idx_agents_auth_source ON agents(auth_source);

-- Immutable interaction event log
CREATE TABLE interactions (
    interaction_id  UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    initiator_id    UUID NOT NULL REFERENCES agents(agent_id),
    counterparty_id UUID NOT NULL REFERENCES agents(agent_id),
    interaction_type TEXT NOT NULL,
    outcome         TEXT NOT NULL,
    context         JSONB DEFAULT '{}',
    evidence_hash   TEXT,
    reported_by     UUID NOT NULL REFERENCES agents(agent_id),
    mutually_confirmed BOOLEAN DEFAULT false,
    reported_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT no_self_report CHECK (initiator_id != counterparty_id)
);
SELECT create_hypertable('interactions', 'reported_at');
CREATE INDEX idx_interactions_initiator ON interactions(initiator_id, reported_at DESC);
CREATE INDEX idx_interactions_counterparty ON interactions(counterparty_id, reported_at DESC);

-- Materialized trust scores
CREATE TABLE trust_scores (
    agent_id        UUID NOT NULL REFERENCES agents(agent_id),
    score_type      TEXT NOT NULL,
    score           NUMERIC(5,4) NOT NULL,
    confidence      NUMERIC(5,4) NOT NULL,
    interaction_count INTEGER NOT NULL,
    factor_breakdown JSONB DEFAULT '{}',
    computed_at     TIMESTAMPTZ DEFAULT now(),
    PRIMARY KEY (agent_id, score_type)
);

-- Disputes
CREATE TABLE disputes (
    dispute_id      UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    interaction_id  UUID NOT NULL REFERENCES interactions(interaction_id),
    filed_by        UUID NOT NULL REFERENCES agents(agent_id),
    filed_against   UUID NOT NULL REFERENCES agents(agent_id),
    reason          TEXT NOT NULL,
    evidence        JSONB DEFAULT '{}',
    status          TEXT DEFAULT 'open',
    resolution      TEXT,
    resolution_note TEXT,
    resolved_by     UUID REFERENCES agents(agent_id),
    resolved_at     TIMESTAMPTZ,
    created_at      TIMESTAMPTZ DEFAULT now()
);
CREATE INDEX idx_disputes_status ON disputes(status) WHERE status = 'open';

-- Signed attestations
CREATE TABLE attestations (
    attestation_id  UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    subject_id      UUID NOT NULL REFERENCES agents(agent_id),
    score_snapshot  JSONB NOT NULL,
    valid_from      TIMESTAMPTZ DEFAULT now(),
    valid_until     TIMESTAMPTZ NOT NULL,
    jwt_token       TEXT NOT NULL,
    revoked         BOOLEAN DEFAULT false,
    created_at      TIMESTAMPTZ DEFAULT now()
);
CREATE INDEX idx_attestations_subject ON attestations(subject_id) WHERE NOT revoked;

-- Alert subscriptions
CREATE TABLE alert_subscriptions (
    subscription_id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    subscriber_id   UUID NOT NULL REFERENCES agents(agent_id),
    watched_agent_id UUID NOT NULL REFERENCES agents(agent_id),
    callback_tool   TEXT NOT NULL,
    threshold_delta NUMERIC(5,4) DEFAULT 0.0500,
    active          BOOLEAN DEFAULT true,
    created_at      TIMESTAMPTZ DEFAULT now(),
    UNIQUE(subscriber_id, watched_agent_id)
);
```

---

## Trust Score Algorithm

### Design Principles

1. **Bayesian foundation** — Beta distribution prior (α=2, β=2), new agents at neutral 0.5
2. **Time decay** — exponential with configurable half-life (default 90 days)
3. **Credibility weighting** — reports from high-trust agents shift scores more
4. **AgentAuth trust level boost** — `root` agents' reports carry 1.2x weight vs `ephemeral`
5. **Mutual confirmation bonus** — interactions reported by both parties carry 1.5x weight
6. **Dispute penalty** — lost disputes apply a multiplicative penalty (configurable, default 0.03 per dispute)
7. **Sybil resistance** — flag and discount correlated reporting patterns

```python
@dataclass
class ScoreComputation:
    prior_alpha: float = 2.0
    prior_beta: float = 2.0
    half_life_days: float = 90.0
    dispute_penalty_per: float = 0.03
    dispute_penalty_floor: float = 0.50
    mutual_confirmation_bonus: float = 1.5

    # AgentAuth trust level weights
    trust_level_weights: dict = field(default_factory=lambda: {
        "root": 1.2,
        "delegated": 1.0,
        "ephemeral": 0.7,
        "standalone": 0.8,   # standalone agents without AgentAuth
    })

    async def compute(self, agent_id: UUID, score_type: str) -> TrustScore:
        interactions = await self.fetch_interactions(agent_id, score_type)

        alpha, beta = self.prior_alpha, self.prior_beta
        now = utc_now()

        for ix in interactions:
            age_days = (now - ix.reported_at).total_seconds() / 86400
            time_weight = 0.5 ** (age_days / self.half_life_days)

            # Reporter credibility: their trust score * their auth trust level
            reporter_trust = await self.get_cached_score(ix.reported_by)
            reporter_auth_level = await self.get_auth_trust_level(ix.reported_by)
            level_weight = self.trust_level_weights.get(reporter_auth_level, 0.8)
            credibility = (0.5 + (reporter_trust * 0.5)) * level_weight

            mutual = self.mutual_confirmation_bonus if ix.mutually_confirmed else 1.0
            w = time_weight * credibility * mutual

            if ix.outcome == "success":
                alpha += w
            elif ix.outcome in ("failure", "timeout"):
                beta += w
            elif ix.outcome == "partial":
                alpha += w * 0.5
                beta += w * 0.5

        lost = await self.count_lost_disputes(agent_id)
        penalty = max(
            1.0 - (lost * self.dispute_penalty_per),
            self.dispute_penalty_floor,
        )

        score = (alpha / (alpha + beta)) * penalty
        confidence = 1.0 - (1.0 / (1.0 + len(interactions) * 0.1))

        return TrustScore(
            score=round(score, 4),
            confidence=round(confidence, 4),
            interaction_count=len(interactions),
            factor_breakdown={
                "bayesian_raw": round(alpha / (alpha + beta), 4),
                "dispute_penalty": round(penalty, 4),
                "interactions_weighted": len(interactions),
                "lost_disputes": lost,
            },
        )
```

---

## Project Structure

```
agent-trust/
├── pyproject.toml
├── uv.lock
├── CLAUDE.md
├── README.md
├── Dockerfile
├── docker-compose.yml
│
├── alembic/
│   ├── alembic.ini
│   ├── env.py
│   └── versions/
│
├── src/
│   └── agent_trust/
│       ├── __init__.py
│       ├── server.py                  # FastMCP server entry point + tool registration
│       ├── config.py                  # pydantic-settings: DB, Redis, AgentAuth MCP, scoring
│       │
│       ├── auth/                      # Authentication layer
│       │   ├── __init__.py
│       │   ├── provider.py           # AuthProvider protocol + factory
│       │   ├── agentauth.py          # AgentAuth MCP client (introspect_token, check_permission)
│       │   ├── standalone.py         # Ed25519 standalone auth fallback
│       │   ├── identity.py           # AgentIdentity model + resolve_identity()
│       │   └── cache.py              # Token introspection caching (Redis)
│       │
│       ├── tools/                     # MCP tool implementations
│       │   ├── __init__.py
│       │   ├── agents.py             # register, profile, search, whoami, link_agentauth
│       │   ├── scoring.py            # check_trust, breakdown, compare
│       │   ├── interactions.py       # report, history
│       │   ├── attestations.py       # issue, verify
│       │   ├── disputes.py           # file, resolve
│       │   └── alerts.py             # subscribe
│       │
│       ├── resources/                 # MCP resource implementations
│       │   ├── __init__.py
│       │   ├── scores.py
│       │   ├── history.py
│       │   ├── leaderboard.py
│       │   └── health.py
│       │
│       ├── prompts/                   # MCP prompt templates
│       │   ├── __init__.py
│       │   ├── evaluate.py
│       │   └── diagnose.py
│       │
│       ├── models/                    # SQLAlchemy ORM models
│       │   ├── __init__.py
│       │   ├── agent.py
│       │   ├── interaction.py
│       │   ├── trust_score.py
│       │   ├── dispute.py
│       │   ├── attestation.py
│       │   └── alert_subscription.py
│       │
│       ├── schemas/                   # Pydantic models for internal validation
│       │   ├── __init__.py
│       │   ├── agent.py
│       │   ├── interaction.py
│       │   ├── score.py
│       │   └── dispute.py
│       │
│       ├── engine/                    # Core business logic
│       │   ├── __init__.py
│       │   ├── score_engine.py       # Bayesian scoring algorithm
│       │   └── sybil_detector.py     # Correlated reporting pattern detection
│       │
│       ├── workers/                   # Background tasks (arq)
│       │   ├── __init__.py
│       │   ├── score_recomputer.py
│       │   ├── decay_refresh.py
│       │   ├── alert_dispatcher.py
│       │   └── attestation_expiry.py
│       │
│       ├── crypto/                    # Attestation signing (service-level)
│       │   ├── __init__.py
│       │   ├── keys.py               # Ed25519 key management for attestations
│       │   └── jwt.py                # JWT signing/verification
│       │
│       └── db/
│           ├── __init__.py
│           ├── session.py             # Async SQLAlchemy session factory
│           └── redis.py               # Redis connection pool
│
├── tests/
│   ├── conftest.py                    # Fixtures: DB, Redis, MCP client, AgentAuth MCP mock
│   ├── factories.py                   # Agent, interaction, dispute factories
│   ├── mocks/
│   │   └── agentauth.py             # Mock AgentAuth MCP tool responses for testing
│   │
│   ├── test_auth/                     # Auth layer tests
│   │   ├── test_agentauth_provider.py
│   │   ├── test_standalone_provider.py
│   │   ├── test_identity_resolution.py
│   │   └── test_token_cache.py
│   │
│   ├── test_tools/                    # MCP tool-level tests
│   │   ├── test_agent_tools.py
│   │   ├── test_scoring_tools.py
│   │   ├── test_interaction_tools.py
│   │   ├── test_attestation_tools.py
│   │   └── test_dispute_tools.py
│   │
│   ├── test_engine/                   # Score algorithm tests
│   │   ├── test_score_engine.py      # Property-based (hypothesis)
│   │   └── test_sybil_detector.py
│   │
│   └── test_integration/              # End-to-end flows
│       ├── test_agentauth_flow.py    # Full flow with real AgentAuth MCP calls
│       ├── test_standalone_flow.py   # Full flow with Ed25519
│       └── test_link_upgrade.py      # Standalone → AgentAuth migration
│
└── scripts/
    ├── generate_keypair.py            # Generate Ed25519 service keypair
    ├── register_scopes.py            # Register trust.* scopes in AgentAuth
    └── seed_test_agents.py
```

---

## Implementation Phases

### Phase 1 — MCP Server + AgentAuth Identity (Tasks 1–6)

**Goal:** Running MCP server where agents authenticate via AgentAuth and register trust profiles.

**Task 1: Project scaffold**

```
Initialize the project:
- Create the full directory structure from the project plan
- Set up pyproject.toml with dependencies:
  - mcp[cli] (MCP SDK with FastMCP)
  - sqlalchemy[asyncio], asyncpg, alembic
  - pydantic-settings, redis[hiredis], structlog
  - pynacl (Ed25519 for attestations), pyjwt
  - mcp[cli] includes MCP client SDK (for AgentAuth MCP calls)
  - arq (background workers)
  - Dev: pytest, pytest-asyncio, hypothesis, factory-boy, ruff
- Create docker-compose.yml: PostgreSQL 16 + TimescaleDB + Redis 7
- Create project README
- Create src/agent_trust/config.py with pydantic-settings including:
  - AUTH_PROVIDER: Literal["agentauth", "standalone", "both"]
  - AGENTAUTH_MCP_URL: str = "https://agentauth.radi.pro/mcp"
  - AGENTAUTH_ACCESS_TOKEN: str (for authenticating MCP tool calls)
- Verify: uv sync succeeds, docker compose up -d starts cleanly
```

**Task 2: Database models + migrations**

```
Create SQLAlchemy async models for all tables from the data model section.
Key difference from previous plan: the agents table has auth_source,
public_key (nullable), and agentauth_linked fields.

Create all model files in src/agent_trust/models/.
Create src/agent_trust/db/session.py with async session factory.
Set up Alembic with async support. Generate initial migration.
Create TimescaleDB hypertable for interactions.

Verify: uv run alembic upgrade head succeeds, all tables exist.
```

**Task 3: AgentAuth integration layer**

```
Create the auth/ module — this is the core integration:

src/agent_trust/auth/provider.py:
- Define AuthProvider protocol with methods:
  - authenticate(token_or_key) -> AgentIdentity
  - check_scope(identity, required_scope) -> bool
  - check_permission(identity, action, resource) -> bool

src/agent_trust/auth/agentauth.py:
- AgentAuthProvider class implementing AuthProvider
- Uses MCP client SDK to call AgentAuth's MCP server (agentauth.radi.pro/mcp):
  - introspect_token MCP tool for token verification
  - check_permission MCP tool for authorization checks
- Returns AgentIdentity with agent_id, scopes, trust_level from introspection
- Handles error cases: expired token, revoked token, MCP connection failure

src/agent_trust/auth/standalone.py:
- StandaloneProvider class implementing AuthProvider
- Ed25519 public key lookup in local DB
- Returns AgentIdentity with limited scopes

src/agent_trust/auth/identity.py:
- AgentIdentity dataclass: agent_id, source, scopes, trust_level
- resolve_identity() function that tries AgentAuth first, standalone second

src/agent_trust/auth/cache.py:
- Redis-based introspection cache
- cached_introspect() with SHA-256 key hashing
- TTL = min(token_expiry, 300 seconds)

Create tests/mocks/agentauth.py:
- Mock AgentAuth responses for testing without hitting real API

Create tests/test_auth/:
- test_agentauth_provider.py: mock introspection responses, verify identity extraction
- test_standalone_provider.py: Ed25519 key registration + lookup
- test_identity_resolution.py: dual-auth resolution logic
- test_token_cache.py: cache hit, miss, expiry

Verify: All auth tests pass. AgentAuth provider correctly extracts
identity from mocked introspection responses.
```

**Task 4: FastMCP server entry point**

```
Create src/agent_trust/server.py:
- Initialize FastMCP("AgentTrust", json_response=True, stateless_http=True)
- Starlette lifespan context manager:
  - Database session pool
  - Redis connection pool
  - MCP client session for AgentAuth (agentauth.radi.pro/mcp)
  - arq worker pool
- Support both transports via CLI args
- Wire up structlog

Verify: Server starts, MCP Inspector connects, list_tools returns empty.
```

**Task 5: Agent registration + identity tools**

```
Create src/agent_trust/tools/agents.py:
- register_agent: dual-path registration (AgentAuth token or Ed25519 key)
  - AgentAuth path: introspect token → extract agent_id → create local profile
  - Standalone path: validate public key → generate UUID → create profile
  - If agent already exists, return existing profile
- link_agentauth: merge standalone profile into AgentAuth identity
- whoami: return identity + trust summary from caller's auth
- get_agent_profile: public profile lookup
- search_agents: filtered search with score/capability criteria

Register all tools in server.py.
Write tests using mocked AgentAuth responses.

Verify: MCP Inspector shows 5 agent tools. Can register via both paths.
```

**Task 6: Test infrastructure**

```
Create tests/conftest.py:
- Test database fixture (separate DB, fresh per session)
- Redis fixture
- MCP client fixture (spawns server subprocess, calls tools via protocol)
- AgentAuth mock fixture (mock MCP client for introspect_token/check_permission tools)

Create tests/factories.py:
- AgentFactory: with auth_source variants (agentauth, standalone)
- InteractionFactory, DisputeFactory

Verify: Integration test registers agent via MCP client, gets profile back.
```

**Acceptance criteria:**

- Agent authenticates with AgentAuth token → profile auto-created with correct agent_id
- Agent registers with Ed25519 key → standalone profile created
- link_agentauth merges standalone profile into AgentAuth identity
- whoami returns correct identity source and scopes
- Token introspection cached in Redis (verify with second call)
- All auth tests pass with mocked AgentAuth

---

### Phase 2 — Interactions + Score Engine (Tasks 7–11)

**Goal:** Agents report interactions, scores computed with AgentAuth trust level weighting.

**Task 7: Interaction reporting tool**

```
Create src/agent_trust/tools/interactions.py:
- report_interaction: REQUIRES authentication (access_token mandatory)
  - Resolve caller identity via auth layer
  - Require trust.report scope (checked via AgentAuth scopes)
  - Validate both agents exist
  - Check for matching counterparty report → set mutually_confirmed
  - Insert interaction, enqueue score recomputation
- get_interaction_history: optional auth, filters, pagination

Write tests. Verify scope enforcement: agent without trust.report gets rejected.
```

**Task 8: Score engine with AgentAuth trust level weighting**

```
Create src/agent_trust/engine/score_engine.py:
- ScoreComputation class from the algorithm section
- KEY INTEGRATION: get_auth_trust_level() checks the reporter's
  auth_source and trust_level from their agent profile
  - AgentAuth root agents: 1.2x weight
  - AgentAuth delegated: 1.0x
  - Standalone: 0.8x
  - Ephemeral: 0.7x
- This means reports from well-established AgentAuth agents
  influence scores more than anonymous standalone reporters

Create tests/test_engine/test_score_engine.py:
- Property-based tests (hypothesis):
  - Score always in [0.0, 1.0]
  - Confidence increases with interaction count
  - All-success → score > 0.7
  - All-failure → score < 0.3
  - Zero interactions → 0.5 with low confidence
- AgentAuth-specific tests:
  - Root reporter shifts score more than ephemeral reporter
  - Mutually confirmed interaction shifts score more than one-sided
```

**Task 9: Scoring tools**

```
Create src/agent_trust/tools/scoring.py:
- check_trust: cached score lookup, fallback to real-time compute
  - Unauthenticated: basic score + confidence
  - Authenticated: adds factor_breakdown summary
- get_score_breakdown: requires auth (trust.read scope)
- compare_agents: batch lookup, ranked output, max 10

Redis cache with 60-second TTL for scores.
```

**Task 10: Background workers**

```
Create src/agent_trust/workers/score_recomputer.py:
- arq worker: listen for events, recompute scores, update DB + cache
- Batch both agents in an interaction

Create worker entrypoint, add to docker-compose.yml.
Verify: interaction report → score updates within 2 seconds.
```

**Task 11: Score resources**

```
Create resources:
- trust://agents/{id}/score
- trust://agents/{id}/history
- trust://leaderboard/{score_type}

Verify in MCP Inspector.
```

**Acceptance criteria:**

- Report 10 successful interactions → score rises above 0.7
- Reports from root-level AgentAuth agents shift scores more
- Mutually confirmed interactions carry more weight
- Scope enforcement: trust.report required to report interactions
- Background recomputation within 2 seconds
- Property-based tests pass with 1000+ examples

---

### Phase 3 — Attestations + Disputes with AgentAuth RBAC (Tasks 12–16)

**Goal:** Portable attestations, dispute resolution gated by AgentAuth permissions.

**Task 12: Attestation signing**

```
Create src/agent_trust/crypto/:
- keys.py: Ed25519 service keypair management
- jwt.py: sign attestations with service key, embed AgentAuth agent_id
  in JWT claims so verifiers can cross-reference

Create scripts/generate_keypair.py
```

**Task 13: Attestation tools**

```
Create src/agent_trust/tools/attestations.py:
- issue_attestation: requires auth + trust.attest.issue scope
  - Snapshot current scores
  - Sign JWT with service key
  - Include agentauth_linked flag + agent_type in claims
- verify_attestation: NO auth required (portable by design)
  - Verify signature, check expiry, check revocation

Tests including expired/revoked/tampered attestation rejection.
```

**Task 14: Dispute tools with AgentAuth permission gating**

```
Create src/agent_trust/tools/disputes.py:
- file_dispute: requires auth + trust.dispute.file scope
- resolve_dispute: requires auth + trust.dispute.resolve scope
  PLUS AgentAuth permission check:
    - Call check_permission(agent_id, "execute", "/trust/disputes/resolve")
    - This uses AgentAuth's policy engine to verify the caller
      is actually authorized as an arbitrator
  This is the key AgentAuth integration point for RBAC:
  AgentTrust defines WHAT permissions exist (scopes),
  AgentAuth enforces WHO has them (policies + delegations).

Tests: verify unauthorized agent gets rejected, authorized arbitrator succeeds.
```

**Task 15: Dispute impact on scores**

```
Update score_engine.py with dispute penalty logic.
Dismissed disputes penalize filer slightly (0.01).
Upheld disputes penalize target (0.03 per dispute, floored at 0.50).
Dispute resolution triggers immediate recomputation for both parties.
```

**Task 16: Remaining resources + attestation expiry worker**

```
- trust://agents/{id}/attestations resource
- trust://disputes/{id} resource
- workers/attestation_expiry.py: periodic revocation of expired attestations
```

**Acceptance criteria:**

- Attestation JWT contains AgentAuth agent_id in claims
- verify_attestation works without authentication
- resolve_dispute rejected for agents without trust.dispute.resolve scope
- resolve_dispute additionally checks AgentAuth check_permission
- Upheld dispute reduces offending agent's score
- Expired attestations auto-revoked by background worker

---

### Phase 4 — Prompts, Alerts, Sybil Detection (Tasks 17–20)

**Goal:** Reasoning templates, notifications, anti-gaming measures.

**Task 17: MCP prompts**

```
Create prompt implementations:
- evaluate_counterparty: multi-step trust assessment
- explain_score_change: diagnostic investigation

Register in server.py. Verify in MCP Inspector.
```

**Task 18: Alert subscriptions + dispatcher**

```
- subscribe_alerts tool: requires auth + trust.admin scope
- workers/alert_dispatcher.py: compare old vs new scores after recomputation
```

**Task 19: Sybil detection**

```
Create src/agent_trust/engine/sybil_detector.py:
- Ring reporting detection (A ↔ B mutual positive feedback loop)
- Burst registration detection (many agents, similar patterns)
- Flag suspicious agents, reduce their credibility weight
- AgentAuth integration: check if flagged agents share a delegation chain
  (agents delegated by the same parent, all positively reporting each other)
```

**Task 20: Rate limiting**

```
Redis-based sliding window rate limits:
- report_interaction: 100/hour per agent
- register_agent: 10/hour per key
- check_trust: 1000/hour per agent
- issue_attestation: 50/hour per agent
- AgentAuth agents with higher trust levels get higher limits
  (root: 2x multiplier, delegated: 1x, ephemeral: 0.5x)
```

**Acceptance criteria:**

- Prompts visible in MCP Inspector
- Sybil detector flags ring-reporting
- Rate limits adjust based on AgentAuth trust level
- Alert fires on score change above threshold

---

### Phase 5 — Hardening & Production (Tasks 21–24)

**Goal:** Production-ready with observability, performance, deployment.

**Task 21: Structured logging + health**

```
- structlog JSON logging with correlation IDs
- trust://health resource: DB, Redis, AgentAuth MCP reachability
- AgentAuth MCP connectivity check in health endpoint
```

**Task 22: AgentAuth scope registration script**

```
Create scripts/register_scopes.py:
- Calls AgentAuth MCP tools to register all trust.* scopes
- Idempotent — safe to run multiple times
- Documents each scope's purpose

This is the "setup" step that makes AgentAuth aware of
AgentTrust's permission model.
```

**Task 23: Performance + load testing**

```
- Redis cache hit rate > 95% for score queries
- Token introspection cache reduces AgentAuth MCP calls by > 80%
- Load test: 1000 check_trust/sec cached, p99 < 50ms
- Load test: 100 report_interaction/sec, p99 < 200ms
- Monitor AgentAuth MCP introspection latency under load
```

**Task 24: Documentation + deployment**

```
- README: architecture, quickstart, tool catalog, AgentAuth setup guide
- Dockerfile: multi-stage build
- docker-compose.yml: full stack including AgentAuth MCP connectivity
- Example script: register agent via AgentAuth MCP, use token with AgentTrust
- AgentAuth setup guide: how to register scopes via MCP, create arbitrator policies
```

**Acceptance criteria:**

- Health endpoint checks AgentAuth MCP reachability
- Scope registration script works against live AgentAuth MCP
- p99 < 50ms for cached score queries
- Token cache reduces AgentAuth MCP calls by > 80%
- README includes AgentAuth setup instructions

---

## AgentAuth Setup Guide (for deployment)

### 1. Register AgentTrust as a service agent in AgentAuth

Use AgentAuth's MCP server to register AgentTrust itself as a tool agent:

```python
# Via MCP client SDK (preferred — dogfooding the MCP approach)
from mcp import ClientSession
from mcp.client.streamable_http import streamablehttp_client

async with streamablehttp_client("https://agentauth.radi.pro/mcp") as (r, w, _):
    async with ClientSession(r, w) as session:
        await session.initialize()
        result = await session.call_tool("quickstart", {
            "name": "AgentTrust",
            "agent_type": "tool",
            "description": "Reputation and trust scoring service for agents"
        })
        # Save the returned api_key and access_token
```

### 2. Register trust-specific scopes

```bash
# Run the scope registration script (uses AgentAuth MCP tools internally)
uv run python scripts/register_scopes.py

# This registers:
# trust.read, trust.report, trust.dispute.file,
# trust.dispute.resolve, trust.attest.issue, trust.admin
```

### 3. Create arbitrator policy in AgentAuth

```python
# Use AgentAuth's check_permission tool to verify policy setup
# Policy creation is done via AgentAuth's admin MCP tools
result = await session.call_tool("check_permission", {
    "agent_id": "<arbitrator-agent-uuid>",
    "action": "execute",
    "resource": "/trust/disputes/resolve",
    "access_token": admin_token
})
# If not allowed, configure policies via AgentAuth admin tools
```

### 4. Grant trust scopes to agents

Agents that want to use AgentTrust need the appropriate scopes in their AgentAuth credentials. When creating a credential via AgentAuth's MCP tools:

```python
result = await session.call_tool("create_credential", {
    "agent_id": "...",
    "access_token": admin_token,
    "scopes": ["trust.read", "trust.report", "trust.dispute.file"]
})
```

---

## Key Design Decisions

**Why AgentAuth instead of rolling our own identity?**
AgentAuth already solves identity, credential management, delegation chains, and policy-based authorization. Building a second identity system inside AgentTrust would fragment the agent ecosystem — agents would need separate credentials for each service. With AgentAuth integration, an agent registers once and uses that identity everywhere.

**Why keep standalone fallback?**
Not every agent is on AgentAuth yet. The standalone path ensures AgentTrust is useful from day one for any agent with an Ed25519 key. It also makes testing simpler and keeps the service independently deployable. The `link_agentauth` tool provides a smooth upgrade path.

**Why introspection caching instead of JWT validation?**
AgentAuth tokens are JWTs signed with RS256/ES256. We could validate locally using AgentAuth's JWKS endpoint. However, calling `introspect_token` via AgentAuth's MCP server also checks the JTI blocklist (revoked tokens) which local validation cannot. The 5-minute cache TTL is a reasonable tradeoff: tokens revoked in the last 5 minutes might still work, but the window is small and matches typical operational patterns.

**Why does AgentAuth trust level influence scoring?**
An agent authenticated as `root` through AgentAuth has been through a more rigorous identity process than a standalone ephemeral agent. Their reports should carry more weight — they have more skin in the game and are more accountable. This creates a natural incentive for agents to upgrade from standalone to AgentAuth.

**Why delegate dispute resolution RBAC to AgentAuth?**
Dispute resolution is the most sensitive operation — it directly affects agents' trust scores. Rather than implementing our own authorization logic, we call AgentAuth's `check_permission` MCP tool. This means the same policy framework that governs all other agent permissions also governs who can arbitrate disputes. The trust service defines what permissions exist (scopes), AgentAuth enforces who has them (policies).

**Why MCP-only, no REST?**
Agents discover tools through `list_tools`. Tool docstrings are the API docs. MCP handles transport negotiation. Adding REST creates a parallel surface to maintain that no agent will use. If a non-MCP system needs access, it can use the MCP client SDK. This principle extends to our own integration with AgentAuth — we connect to AgentAuth as an MCP client rather than calling its REST API, dogfooding the MCP-first philosophy.

---

## Risk Mitigation

| Risk                         | Mitigation                                                                                                                    |
| ---------------------------- | ----------------------------------------------------------------------------------------------------------------------------- |
| AgentAuth unavailable        | Introspection cache continues serving cached tokens; standalone auth unaffected; health endpoint alerts on AgentAuth MCP down |
| Sybil attacks                | Credibility weighting + AgentAuth trust level boost + pattern detection + delegation chain analysis                           |
| Score gaming                 | Mutual confirmation bonus incentivizes both-party reporting; one-sided reports discounted; standalone agents weighted lower   |
| Token replay                 | Short cache TTL (5 min) + AgentAuth JTI blocklist; attestations have separate revocation                                      |
| Scope escalation             | AgentAuth enforces scope attenuation on delegations; AgentTrust verifies scopes on every tool call                            |
| AgentAuth migration friction | link_agentauth provides one-step upgrade; history and scores transfer cleanly                                                 |
| Scaling                      | Stateless HTTP + Redis cache + async workers = horizontal scaling; AgentAuth MCP calls are cached                             |

---

## Success Metrics

| Metric                   | Target (Month 3)            | Target (Month 6)           |
| ------------------------ | --------------------------- | -------------------------- |
| Registered agents        | 50                          | 500+                       |
| AgentAuth-linked agents  | 30 (60%)                    | 400 (80%)                  |
| Interactions/day         | 1K                          | 50K                        |
| check_trust p99 (cached) | < 50ms                      | < 20ms                     |
| AgentAuth cache hit rate | > 80%                       | > 95%                      |
| Framework integrations   | Claude Desktop, Claude Code | LangChain, CrewAI, AutoGen |

---

## Environment Setup

```bash
# Initialize
uv init agent-trust
cd agent-trust

# Add dependencies
uv add "mcp[cli]" sqlalchemy[asyncio] asyncpg alembic
uv add pydantic-settings "redis[hiredis]" structlog
uv add pynacl pyjwt arq
uv add starlette "uvicorn[standard]"

# Add dev dependencies
uv add --dev pytest pytest-asyncio hypothesis factory-boy ruff

# Infrastructure
docker compose up -d  # PostgreSQL + TimescaleDB + Redis

# Setup
uv run alembic upgrade head
uv run python scripts/generate_keypair.py
uv run python scripts/register_scopes.py  # register trust.* in AgentAuth via MCP

# Run (local dev with stdio)
uv run python -m agent_trust.server

# Run (remote with Streamable HTTP)
uv run python -m agent_trust.server --transport streamable-http --port 8000

# Test with MCP Inspector
uv run mcp dev src/agent_trust/server.py
```

---

## Recommended Claude Code Session Flow

| Session | Tasks       | Focus                                                                |
| ------- | ----------- | -------------------------------------------------------------------- |
| 1       | Tasks 1–2   | Scaffold, models, migrations                                         |
| 2       | Tasks 3–4   | AgentAuth integration layer + server entry point                     |
| 3       | Tasks 5–6   | Agent tools + test infrastructure                                    |
| 4       | Tasks 7–9   | Interactions, score engine with trust level weighting, scoring tools |
| 5       | Tasks 10–11 | Background workers, resources                                        |
| 6       | Tasks 12–14 | Attestations, disputes with AgentAuth RBAC                           |
| 7       | Tasks 15–17 | Score impact, resources, prompts                                     |
| 8       | Tasks 18–20 | Alerts, sybil detection, rate limiting                               |
| 9       | Tasks 21–24 | Logging, scope registration, performance, docs                       |

Start each session by reading CLAUDE.md. End each session with `uv run pytest` passing. Each task is a self-contained prompt — copy-paste the task description into Claude Code.

---

_This plan is designed for sequential execution with Claude Code. Each numbered task produces working, tested code. Start with Task 1 and proceed in order — later tasks build on earlier outputs._
