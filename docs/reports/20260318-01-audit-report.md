> **Status:** All 14 vulnerabilities resolved — see [Resolution Summary](#resolution-summary) below.
> **Fixed:** 2026-03-18

🔴 CRITICAL Vulnerabilities

1. Sybil Attack — Score Inflation via Sock Puppets

Impact: A rogue agent creates N fake agents and has them all report outcome: "success" against the
rogue. Each fake-to-rogue interaction inflates alpha in the Bayesian score. With the default prior
(α=2, β=2), just 10 positive interactions from 10 sock puppets pushes the rogue's score to ~0.85+.

Why current defenses are insufficient:

- SybilDetector exists but get_sybil_credibility_multiplier() is never called from
  ScoreComputation.compute(). The sybil module is informational only — it doesn't affect scores.
- Ring detection requires ≥2 mutual pairs. A rogue can avoid this by making sock puppets report
  one-way only.
- register_agent has no rate limiting — unlimited registrations.

Fix:

1.  Integrate sybil multiplier into score engine — call get_sybil_credibility_multiplier(reporter_id)
    in compute() and multiply it into the credibility weight.
2.  Rate-limit register_agent per IP/subnet and per auth token.
3.  Require minimum age before an agent's reports carry weight (e.g., 24h cooling-off after
    registration).
4.  One-way ring detection — flag agents that receive many positive reports from agents they've never
    interacted with.

---

2. Mutual Confirmation Collusion

Impact: Two colluding agents can trivially get the 1.5× credibility bonus on every interaction by
always reporting the same outcome. The mutually_confirmed check (interactions.py:113-121) only
verifies that both parties reported, not that the interaction actually happened.

Why it's dangerous: The bonus is multiplicative and stacks across all interactions, so two colluding
agents permanently inflate each other's scores 50% faster than honest agents.

Fix:

1.  Cap mutual confirmation benefit — apply diminishing returns when the same pair mutually confirms
    repeatedly (e.g., after the 5th mutual confirmation in 30 days, bonus drops to
    1.1×).
2.  Diversity requirement — weight mutual confirmations lower if an agent's interactions are
    concentrated among few counterparties.
3.  Evidence requirements — require evidence_hash for mutual confirmation bonus.

---

3. Rate Limiter Fails Open

Impact: If Redis goes down, check_rate_limit() returns allowed=True (see ratelimit.py fallback). A
rogue agent can DoS Redis (or wait for an outage) and then flood unlimited interactions.

Fix:

1.  Fail closed — if Redis is unreachable, deny the request or fall back to an in-memory counter with
    aggressive limits.
2.  Circuit breaker — after N consecutive Redis failures, switch to a degraded mode that requires
    authentication with elevated trust levels.

---

4. Legacy Public Key Authentication — No Proof of Ownership

Impact: standalone.py:99-120 — authenticating via public_key_hex only does a database lookup. Public
keys are not secrets. Any agent that discovers another agent's public key (it's in metadata, logs, or
attestation JWTs) can impersonate that agent.

Why it's exploitable: Public keys are 32 bytes / 64 hex chars. They appear in tool responses (
register_agent returns public_key_hex), in logs, and can be brute-force iterated from the database
index.

Fix:

1.  Remove the legacy path entirely — require signed JWTs for all standalone auth. The code already
    marks it warning="no_crypto_proof".
2.  If keeping it, require a challenge-response — server sends a nonce, client signs it with the
    private key.

---

🟠 HIGH Vulnerabilities

5. Targeted Score Suppression (Griefing)

Impact: A rogue agent reports outcome: "failure" against a target agent. Because is_reporter is
checked and self-reported failures are ignored (score_engine.py:88-90), only third-party failure
reports affect scores. But the rogue IS a third party to the target. A rogue can create sock puppets
to mass-report failures against any agent.

Current protection: Reporter credibility weighs by the rogue's own score (low-trust reporters have
less impact). But new agents default to 0.5 trust — enough to cause meaningful damage before their own
score drops.

Fix:

1.  Minimum reporter interaction count — reports from agents with < N interactions carry zero weight.
2.  Anomaly detection — flag agents whose failure-reporting rate against a single target is abnormally
    high.
3.  Counter-reporting weight — if the target disputes, auto-reduce weight of unconfirmed negative
    reports pending resolution.

---

6. Dispute Weaponization

Impact: A rogue with trust.dispute.file scope can file disputes against every interaction it's party
to, creating operational burden and locking up arbitrator resources. The dismissed penalty is only
−0.01 per dispute (floor 0.90), meaning a rogue can file ~10 frivolous disputes before hitting the
floor, each one requiring manual arbitration.

Fix:

1.  Exponential penalty for repeated dismissed disputes — 1st: −0.01, 2nd: −0.02, 3rd: −0.04, etc.
2.  Auto-dismiss disputes from agents with ≥3 prior dismissed disputes.
3.  Cooldown period — after a dismissed dispute, block the filer from filing again for 24–72h.

---

7. Interaction Flooding Without Counterparty Consent

Impact: A rogue can unilaterally report interactions with any registered agent. There's no requirement
for the counterparty to have agreed to or initiated the interaction. This means a rogue can generate
thousands of one-sided interaction records against a target.

Why it matters: While one-sided reports carry less weight (no mutual confirmation bonus), they still
affect the score engine. A flood of outcome: "failure" reports by a rogue (as reporter) against a
target will accumulate in \_fetch_interactions().

Fix:

1.  Per-pair rate limit — cap the number of interactions one agent can report about a specific
    counterparty (e.g., max 10/day per pair).
2.  Counterparty notification — notify the counterparty and allow them to flag spurious reports.
3.  Pending state — one-sided reports enter a "pending" state for 24h; if the counterparty doesn't
    confirm or the reporter's own trust is low, reduce their weight further.

---

8. trust_level Metadata Spoofing

Impact: In score*engine.py:204-205, an agent's auth trust level is read from
agent.metadata*.get("trust*level", "delegated"). The metadata* field is user-supplied JSONB set during
register_agent. A rogue can register with metadata={"trust_level": "root"} and get the 1.2× reporter
credibility weight instead of 0.8×.

Fix:

1.  Never read security-critical fields from user-supplied metadata — trust*level should be derived
    from auth_source and the AgentAuth introspection result, never from metadata*.
2.  Store the trust level as a first-class column set by the auth layer, not by user input.

---

🟡 MEDIUM Vulnerabilities

9. Attestation Score Replay

Impact: Attestations contain a score_snapshot frozen at issuance time. A rogue whose score drops can
present an old (valid, not-yet-expired) attestation showing a higher score. Attestations can have up
to 720h (30-day) TTL.

Fix:

1.  Shorter default TTL — reduce max to 72h.
2.  Include score version/timestamp in the JWT claims so verifiers can check freshness.
3.  Proactive revocation — when a score drops significantly (>0.1), auto-revoke outstanding
    attestations for that agent.

---

10. No Duplicate Interaction Detection

Impact: The mutual confirmation check (interactions.py:113-121) only checks for a counterparty report.
There's no check preventing the same agent from reporting the same interaction multiple times. A rogue
can call report_interaction repeatedly with the same counterparty and type, creating many interaction
records.

Why rate limiting isn't enough: Rate limits allow 60 req/min for standalone agents. Over a day that's
86,400 interactions — far more than needed to dominate a target's score history.

Fix:

1.  Deduplication window — reject reports from the same (reporter, counterparty, type) pair within a
    configurable window (e.g., 1 hour).
2.  Daily per-pair cap — max N interactions between the same pair per day.

---

11. Delegation Chain Cycles

Impact: Agent.delegated_by is a self-referencing FK with no cycle prevention. An agent A delegated_by
B delegated_by C delegated_by A creates an infinite loop. The sybil detector caps at 10 hops but the
delegated_by field itself is writable.

Fix:

1.  Validate on write — before setting delegated_by, walk the chain to ensure no cycle.
2.  Max depth enforcement — reject delegation if it would create a chain >
3.

---

12. Unbounded JSONB Fields

Impact: metadata\_, context, evidence, factor_breakdown are arbitrary JSONB with no size limits. A
rogue can store multi-MB payloads, causing storage bloat and slow queries.

Fix:

1.  Size limits — validate JSONB payload size (e.g., max 10KB per field).
2.  Schema validation — define allowed keys and value types for context and metadata\_.

---

13. Information Leakage via Unauthenticated Endpoints

Impact: get_interaction_history, check_trust, get_agent_profile, and all MCP resources are accessible
without authentication. A rogue can enumerate all agent IDs, map the entire trust graph, identify
high-value targets, and monitor score changes to time attacks.

Fix:

1.  Require auth for interaction history — or at minimum, redact counterparty IDs for unauthenticated
    callers.
2.  Rate-limit unauthenticated reads more aggressively (currently 10/min but per-tool, not global).

---

14. callback_tool Injection in Alert Subscriptions

Impact: AlertSubscription.callback_tool is a free-text field (200 chars). When alerts fire, the system
dispatches to this tool name. A rogue could subscribe with a callback_tool value that references a
dangerous MCP tool or an external endpoint, potentially triggering unintended actions when scores
change.

Fix:

1.  Allowlist callback tools — maintain a registry of permitted callback tool names.
2.  Validate tool exists at subscription time.
3.  Sandbox dispatching — run callbacks with minimal permissions.

---

Summary Matrix

| # | Vulnerability | Severity | Exploitability | Status |
|---|---|---|---|---|
| 1 | Sybil score inflation | 🔴 Critical | Easy | ✅ Fixed |
| 2 | Mutual confirmation collusion | 🔴 Critical | Easy | ✅ Fixed |
| 3 | Rate limiter fails open | 🔴 Critical | Medium | ✅ Fixed |
| 4 | Legacy pubkey impersonation | 🔴 Critical | Medium | ✅ Fixed |
| 5 | Targeted score suppression | 🟠 High | Easy | ✅ Fixed |
| 6 | Dispute weaponization | 🟠 High | Easy | ✅ Fixed |
| 7 | Interaction flooding | 🟠 High | Easy | ✅ Fixed |
| 8 | trust_level metadata spoofing | 🟠 High | Easy | ✅ Fixed |
| 9 | Attestation score replay | 🟡 Medium | Medium | ✅ Fixed |
| 10 | Duplicate interactions | 🟡 Medium | Easy | ✅ Fixed |
| 11 | Delegation chain cycles | 🟡 Medium | Easy | ✅ Fixed |
| 12 | Unbounded JSONB | 🟡 Medium | Easy | ✅ Fixed |
| 13 | Info leakage | 🟡 Medium | Easy | ✅ Fixed |
| 14 | Callback tool injection | 🟡 Medium | Medium | ✅ Fixed |

The most impactful fix is #1 — wiring get_sybil_credibility_multiplier() into
ScoreComputation.compute() — because it's already implemented and just needs integration. Combined
with #8 (stop reading trust_level from user metadata), these two changes close the largest attack
surface with minimal code changes.

---

## Resolution Summary

The following changes were implemented to address all identified vulnerabilities.

### Score Engine

| # | Fix Applied |
|---|---|
| 1 | **Sybil integration** — `get_sybil_credibility_multiplier()` is now called inside `ScoreComputation.compute()` for every reporter. Results cached per-reporter within a recomputation. |
| 2 | **Mutual confirmation diminishing returns** — bonus decays as `max(1.5 − 0.1 × (pair_count − 1), 1.0)`. Repeated mutual confirmations between the same pair approach 1.0× instead of always granting 1.5×. |
| 5 | **Reporter interaction count gate** — reporters with fewer than 3 recorded interactions have their credibility multiplied by 0.3, limiting new accounts from immediately influencing scores. |
| 8 | **Trust level derivation hardened** — trust level is now derived exclusively from `auth_source` and `agentauth_linked` server-side fields. User-supplied `metadata_` is never consulted for security decisions. |
| 6 | **Exponential dismissed-dispute penalty** — penalty grows as `0.01 × 1.5^i` per dismissal (previously flat 0.01). The 0.9 floor is reached in ~5–6 dismissals instead of 10. |

### Auth

| # | Fix Applied |
|---|---|
| 4 | **Legacy pubkey path removed** — `_authenticate_public_key_hex()` now raises `AuthenticationError` immediately. All standalone auth requires a signed JWT (proof of private key possession). |

### Rate Limiting

| # | Fix Applied |
|---|---|
| 3 | **Rate limiter fails closed** — both Redis-failure fallback paths in `ratelimit.py` now return `allowed=False, retry_after=10` instead of allowing the request through. |

### Tools

| # | Fix Applied |
|---|---|
| 7 | **Per-pair interaction cap** — max 10 interactions reportable per (reporter, counterparty) pair per 24h. 1-hour deduplication window per (reporter, counterparty, interaction_type). |
| 10 | **Deduplication window** — same as #7; both per-pair daily cap and 1h dedup window address duplicate flooding. |
| 12 | **JSONB size limits** — `context` capped at 10KB; `evidence_hash` validated as 64-character hex SHA-256; `display_name` ≤200 chars; `capabilities` ≤50 items each ≤100 chars; `metadata` ≤10KB. |
| 13 | **Auth required for interaction history** — `get_interaction_history` now requires `access_token` with `trust.read` scope, preventing unauthenticated enumeration of interaction graphs. |
| 11 | **Delegation cycle detection** — `register_agent` and `update_agent` now walk the `delegated_by` chain before setting delegation, rejecting cycles and chains deeper than 5 hops. |
| 6 | **Dispute filing controls** — `file_dispute` is now rate-limited; agents with ≥5 prior dismissed disputes are blocked; a 24h cooldown is enforced after any dismissed dispute. |
| 14 | **Callback tool allowlist** — `subscribe_alerts` validates `callback_tool` against a `PERMITTED_CALLBACK_TOOLS` frozenset. `alert_dispatcher` re-validates before dispatching (defense-in-depth). |

### Workers & Attestations

| # | Fix Applied |
|---|---|
| 9 | **Proactive attestation revocation** — when `recompute_score` detects an overall score drop >0.10, all non-revoked outstanding attestations for that agent are immediately revoked. Default TTL reduced from 24h to 12h; maximum TTL reduced from 720h to 72h. |
