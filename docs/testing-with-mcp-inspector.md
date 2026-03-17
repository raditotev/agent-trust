# Testing AgentTrust with MCP Inspector

MCP Inspector is a browser-based interactive UI for exploring and testing MCP servers. It lets you invoke tools, browse resources, and run prompts without writing any client code.

## Prerequisites

Ensure the following are running before launching Inspector:

```bash
docker compose up -d postgres redis
uv run alembic upgrade head
uv run python scripts/generate_keypair.py   # only needed once
```

## Launch MCP Inspector

```bash
uv run mcp dev src/agent_trust/server.py
```

This starts the server in **stdio** mode and opens the Inspector UI at `http://localhost:5173` (or the URL printed in the terminal). The server connects automatically — no manual configuration needed.

> **Note:** `mcp dev` reads your `.env` file. Copy `.env.example` to `.env` first if you haven't already:
> ```bash
> cp .env.example .env
> ```

---

## Walkthrough: Core Flows

The sections below follow a natural progression. Work through them in order to test the full lifecycle.

MCP Inspector shows a separate input field for each argument — fill them in individually as described below.

### 1. Register an Agent

Navigate to **Tools → `register_agent`** and fill in:

| Field | Value |
|---|---|
| `display_name` | `test-agent-1` |
| `capabilities` | *(leave empty)* |
| `metadata` | *(leave empty)* |
| `access_token` | *(leave empty)* |
| `public_key_hex` | *(leave empty)* |

Copy the returned `agent_id` — you'll use it throughout the rest of the walkthrough.

---

### 2. Look Up the Agent

**Tools → `get_agent_profile`**

| Field | Value |
|---|---|
| `agent_id` | *(paste agent_id from step 1)* |
| `access_token` | *(leave empty)* |

Verify the profile shows `trust_level: standalone` and an initial score around `0.5`.

You can also read the live resource: **Resources → `trust://agents/{id}/score`**

---

### 3. Check Trust Score

**Tools → `check_trust`**

| Field | Value |
|---|---|
| `agent_id` | *(paste agent_id from step 1)* |
| `score_type` | `overall` |
| `access_token` | *(leave empty)* |

Unauthenticated calls are allowed. The response includes `score`, `confidence`, and `interaction_count`.

---

### 4. Register a Second Agent (Reporter)

Repeat step 1 with `display_name` set to `test-agent-reporter`. You need two agents to submit interaction reports.

---

### 5. Report an Interaction

**Tools → `report_interaction`**

> **Auth required:** `trust.report` scope. First use `generate_agent_token` (step 4b below) to get an `access_token` for the reporter.

**4b. Generate a token for the reporter**

**Tools → `generate_agent_token`**

| Field | Value |
|---|---|
| `agent_id` | *(paste agent_id from step 4)* |
| `private_key_hex` | *(paste private_key_hex from step 4)* |
| `ttl_minutes` | `60` |

Copy the returned `access_token` — use it in the next call.

**Now call `report_interaction`:**

| Field | Value |
|---|---|
| `counterparty_id` | *(paste agent_id from step 1 — the agent being reported on)* |
| `interaction_type` | `transaction` |
| `outcome` | `success` |
| `access_token` | *(paste the token from `generate_agent_token` above)* |
| `context` | *(leave empty)* |
| `evidence_hash` | *(leave empty)* |

Valid values for `interaction_type`: `transaction`, `delegation`, `query`, `collaboration`  
Valid values for `outcome`: `success`, `failure`, `timeout`, `partial`

Repeat with a few more reports (mix `success` and `failure` outcomes) to see scoring effects.

---

### 6. Observe Score Change

**Tools → `check_trust`** again (same `agent_id` as step 3). The score and confidence should now reflect the submitted interactions.

For a detailed breakdown: **Tools → `get_score_breakdown`**

| Field | Value |
|---|---|
| `agent_id` | *(paste agent_id from step 1)* |
| `access_token` | *(token with `trust.read` scope)* |

---

### 7. Browse Interaction History

**Tools → `get_interaction_history`**

| Field | Value |
|---|---|
| `agent_id` | *(paste agent_id from step 1)* |
| `interaction_type` | *(leave empty for all types)* |
| `outcome` | *(leave empty for all outcomes)* |
| `since_days` | `90` |
| `limit` | `20` |
| `access_token` | *(leave empty)* |

Copy an `interaction_id` from the response for step 8.

Or use the resource: **Resources → `trust://agents/{id}/history`**

---

### 8. File a Dispute

**Tools → `file_dispute`** (requires `trust.dispute.file` scope)

| Field | Value |
|---|---|
| `interaction_id` | *(paste interaction_id from step 7)* |
| `reason` | `This report is inaccurate — the task was completed successfully` |
| `access_token` | *(token with `trust.dispute.file` scope)* |
| `evidence` | *(leave empty)* |

Copy the returned `dispute_id` for step 9.

---

### 9. Resolve a Dispute

**Tools → `resolve_dispute`** (requires `trust.dispute.resolve` + AgentAuth RBAC arbitrator role)

| Field | Value |
|---|---|
| `dispute_id` | *(paste dispute_id from step 8)* |
| `resolution` | `upheld` |
| `access_token` | *(arbitrator token)* |
| `resolution_note` | `Evidence supports the dispute claim` |

Valid values for `resolution`: `upheld`, `dismissed`, `split`

Resolving as `upheld` applies a score penalty to the agent who filed the original interaction report.

---

### 10. Issue an Attestation

**Tools → `issue_attestation`** (requires `trust.attest.issue` scope)

| Field | Value |
|---|---|
| `agent_id` | *(paste agent_id from step 1)* |
| `access_token` | *(token with `trust.attest.issue` scope)* |
| `ttl_hours` | `24` |

Returns a signed JWT. Copy the `jwt_token` value for the next step.

---

### 11. Verify the Attestation

**Tools → `verify_attestation`** — no auth required.

| Field | Value |
|---|---|
| `jwt_token` | *(paste jwt_token from step 10)* |

Confirms signature validity, expiry, and the embedded score snapshot.

---

### 12. Run Sybil Detection

**Tools → `sybil_check`**

| Field | Value |
|---|---|
| `agent_id` | *(paste agent_id from step 1)* |

Returns risk flags: ring reporting, burst registration, and delegation chain depth.

---

### 13. Compare Multiple Agents

**Tools → `compare_agents`**

| Field | Value |
|---|---|
| `agent_ids` | *(comma-separated list of agent UUIDs, e.g. the two you registered in steps 1 and 4)* |
| `score_type` | `overall` |
| `access_token` | *(leave empty)* |

Returns agents ranked by overall trust score.

---

### 14. Explore Prompts

Navigate to the **Prompts** tab to use the built-in reasoning templates:

| Prompt | What it does |
|---|---|
| `evaluate_counterparty` | Structured PROCEED / CAUTION / DECLINE assessment |
| `explain_score_change` | Diagnose why a score moved |
| `dispute_assessment` | Arbitrator guide for evidence-based resolution |

---

### 15. Check Server Health

**Resources → `trust://health`**

Returns DB and Redis connectivity status, server version, and uptime.

---

## Authentication

AgentTrust supports two authentication paths:

### Standalone Ed25519 (simplest for local testing)

Standalone agents authenticate with a **short-lived JWT they sign themselves** using their Ed25519 private key. This proves ownership of the private key — unlike a plain key lookup, the server verifies the signature before accepting the identity.

**Step 1 — Register an agent** via **Tools → `register_agent`** with any `display_name`. Leave `public_key_hex` empty to auto-generate a keypair. The response includes both `public_key_hex` and `private_key_hex`. Store `private_key_hex` securely — it will not be shown again.

**Step 2 — Generate an access token** via **Tools → `generate_agent_token`**:

| Field | Value |
|---|---|
| `agent_id` | *(paste agent_id from step 1)* |
| `private_key_hex` | *(paste private_key_hex from step 1)* |
| `ttl_minutes` | `60` |

Copy the returned `access_token`.

**Step 3 — Use the token** as `access_token` in any tool that requires auth. When it expires, call `generate_agent_token` again with the same `private_key_hex`.

### AgentAuth Token

Set `AGENTAUTH_ACCESS_TOKEN` in `.env` to a valid token from [agentauth.radi.pro](https://agentauth.radi.pro). AgentTrust will introspect it automatically on each tool call.

### No Auth (Anonymous)

Many tools work without authentication. Anonymous calls are rate-limited to **10 requests/min** and interaction reports submitted without auth receive reduced credibility weight.

---

## Troubleshooting

| Symptom | Likely cause | Fix |
|---|---|---|
| Inspector shows "Connection failed" | Server didn't start | Check terminal for startup errors; ensure `.env` is populated |
| `register_agent` fails with DB error | Postgres not running or migrations not applied | `docker compose up -d postgres && uv run alembic upgrade head` |
| Score doesn't change after reporting | arq worker not running | Start it: `uv run python scripts/run_worker.py` |
| Attestation verification fails | Signing key mismatch | Ensure `SIGNING_KEY_PATH` points to the same key used to start the server |
| Rate limit errors | Too many requests | Wait 60 seconds, or authenticate to get a higher limit |
