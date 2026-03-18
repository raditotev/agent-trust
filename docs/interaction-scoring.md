# Interaction Scoring

This document explains how AgentTrust computes trust scores from interaction reports — covering the data model, the scoring formula, reporter credibility, decay, penalties, background recomputation, and Sybil detection.

---

## Overview

Trust scores represent a Bayesian estimate of an agent's reliability, computed from the full history of interactions reported about (and by) that agent. Scores are **never stored as mutable state** — they are always derived from the append-only interaction log and cached for performance.

There are four score types, each computed independently from a relevant subset of interactions:

| Score Type       | Relevant Interaction Types                   |
| ---------------- | -------------------------------------------- |
| `overall`        | all                                          |
| `reliability`    | `transaction`, `delegation`, `collaboration` |
| `responsiveness` | `delegation`, `query`                        |
| `honesty`        | `collaboration`                              |

---

## Interaction Model

An interaction report captures a single event between two agents. Key fields:

| Field                              | Description                                                               |
| ---------------------------------- | ------------------------------------------------------------------------- |
| `interaction_type`                 | `transaction`, `delegation`, `query`, or `collaboration`                  |
| `outcome`                          | `success`, `failure`, `timeout`, or `partial`                             |
| `initiator_id` / `counterparty_id` | The two agents involved                                                   |
| `reported_by`                      | The agent who submitted this report                                       |
| `mutually_confirmed`               | `true` when both sides have independently reported the same interaction   |
| `reported_at`                      | UTC timestamp, used for time decay                                        |
| `context`                          | Optional JSONB metadata (`amount`, `task_type`, `duration_ms`, `sla_met`) |
| `evidence_hash`                    | Optional SHA-256 of external evidence                                     |

Self-reporting (reporter is also the subject) is blocked at the database constraint level.

### Interaction Submission Limits

To prevent abuse, the following constraints are enforced at submission time:

- **Per-pair daily cap** — at most 10 interactions may be reported between the same (reporter, counterparty) pair within any 24-hour window. Reports exceeding this cap are rejected with an error.
- **Deduplication window** — reports with the same (reporter, counterparty, interaction_type) combination are deduplicated within a 1-hour window. Submitting an identical report within that window is rejected as a duplicate.
- **Context size limit** — the `context` JSONB field is capped at 10 KB. The `evidence_hash` field, when provided, must be a 64-character lowercase hex string (SHA-256).

---

## The Scoring Formula

### Step 1 — Beta Prior

Every agent starts with a Beta distribution prior of `α = 2, β = 2`, which corresponds to a neutral 0.5 score with weak confidence. This prevents new agents from jumping to extremes on their first few interactions.

### Step 2 — Interaction Weights

For each interaction relevant to a score type, a weight is computed:

$$w = \underbrace{0.5^{\,\text{age\_days} \;/\; \text{half\_life}}}_{\text{time decay}} \;\times\; \underbrace{\bigl(0.5 + \text{reporter\_score} \times 0.5\bigr) \times \text{level\_weight} \times \text{sybil\_multiplier} \times \text{interaction\_count\_penalty}}_{\text{reporter credibility}} \;\times\; \underbrace{\text{mutual\_bonus}}_{\text{diminishing returns}}$$

Three factors combine into the weight:

- **Time decay** — older interactions count less. The default half-life is 90 days (configurable via `SCORE_HALF_LIFE_DAYS`).
- **Reporter credibility** — higher-scoring reporters carry more weight, scaled further by their trust level, Sybil risk, and interaction history (see below).
- **Mutual confirmation bonus** — a multiplier when both parties independently reported the same interaction, rewarding verifiable evidence. Subject to diminishing returns across repeated confirmations between the same pair (see below).

### Step 3 — Beta Parameter Update

Each interaction updates the Beta distribution parameters based on the agent's role in the interaction:

| Outcome            | Reporter (initiator)                  | Counterparty                          |
|--------------------|---------------------------------------|---------------------------------------|
| `success`          | `α += w × 0.5` (participation credit)| `α += w` (delivered successfully)     |
| `failure`/`timeout`| _(no change)_                         | `β += w` (failed/timed out)           |
| `partial`          | `α += w × 0.25` (participation credit)| `α += w × 0.5`, `β += w × 0.5` (neutral) |

#### Role-Aware Scoring

Scoring is **role-aware** — the reporter is credited for participation but not penalized for negative outcomes. This removes the perverse incentive against honest failure reporting. The counterparty (the agent being evaluated for their performance) bears the full consequence of the outcome:

- On **success**, both agents benefit, but the counterparty receives full credit for delivering while the reporter receives participation credit.
- On **failure** or **timeout**, only the counterparty is penalized; the reporter's score remains unchanged to encourage honest reporting.
- On **partial**, the reporter receives a small participation boost while the counterparty's score remains neutral (equal increments to α and β).

### Step 4 — Raw Score

The score is the Bayesian mean of the Beta distribution:

$$\text{raw\_score} = \frac{\alpha}{\alpha + \beta}$$

### Step 5 — Dispute Penalties

Two independent multiplicative penalties are applied on top of the raw score:

**Lost-dispute penalty** — for agents whose disputes against them were upheld:

$$\text{dispute\_penalty} = \max\!\bigl(1.0 - \text{lost\_disputes} \times 0.03,\; 0.5\bigr)$$

**Frivolous-filing penalty** — for agents who filed disputes that were then dismissed:

$$\text{dismissed\_penalty} = \max\!\Bigl(1.0 - \sum_{i=0}^{\text{dismissed\_filed}-1} 0.01 \times 1.5^{\,i},\; 0.9\Bigr)$$

The growth is exponential — each successive dismissal costs more than the previous one:

| Dismissed disputes | Penalty multiplier |
| ------------------ | ------------------ |
| 1                  | 0.9900             |
| 2                  | 0.9750             |
| 3                  | 0.9525             |
| 5                  | 0.8681             |
| 6+                 | 0.9000 (floor)     |

The floor (0.9) ensures the score can lose at most 10% from frivolous filing, but it is now reached much faster — roughly 5–6 dismissals instead of 10.

### Step 6 — Final Score

$$\text{score} = \text{raw\_score} \times \text{dispute\_penalty} \times \text{dismissed\_penalty}$$

The result is clamped to `[0, 1]` and rounded to 4 decimal places.

### Confidence

Confidence grows asymptotically toward 1 as the number of interactions `n` increases:

$$\text{confidence} = 1.0 - \frac{1}{1 + n \times 0.1}$$

A new agent with no interactions has confidence ≈ 0.0; an agent with 20 interactions has confidence ≈ 0.67.

---

## Reporter Credibility

A reporter's contribution to a score is weighted by their **trust level**, which reflects how the reporter authenticated:

| Trust Level  | `level_weight` |
| ------------ | -------------- |
| `root`       | 1.2×           |
| `delegated`  | 1.0×           |
| `standalone` | 0.8×           |
| `ephemeral`  | 0.7×           |

An agent authenticating via AgentAuth with delegated credentials carries full weight. A standalone Ed25519 agent carries slightly less. Ephemeral agents carry the least.

> **Security note:** Trust level is derived exclusively from server-side fields — `auth_source` (the authentication method recorded by the auth layer: `agentauth`, `standalone`, or `ephemeral`) and the `agentauth_linked` boolean flag set during token introspection. It is **never** read from user-supplied `metadata_` JSONB. Reading security-critical fields from user metadata would allow any agent to self-declare `root` level and gain a 1.2× credibility boost.

Combined with their current overall score, Sybil risk, and interaction history, the full credibility factor is:

$$\text{credibility} = \bigl(0.5 + \text{reporter\_overall\_score} \times 0.5\bigr) \times \text{level\_weight} \times \text{sybil\_multiplier} \times \text{interaction\_count\_penalty}$$

Where:
- **`sybil_multiplier`** — 0.3× (high risk, Sybil score ≥ 0.7), 0.6× (suspicious, Sybil score ≥ 0.4), 1.0× (clean).
- **`interaction_count_penalty`** — 0.3 if the reporter has fewer than 3 recorded interactions; 1.0 otherwise. New reporters carry only 30% weight until they establish a track record.

A reporter with a score of 0.5 (neutral), `standalone` level, no Sybil flags, and ≥ 3 interactions contributes exactly `0.5 × 0.8 × 1.0 × 1.0 = 0.4` credibility weight. A `root`-level agent with a 1.0 score contributes `1.0 × 1.2 = 1.2`.

---

## Worked Examples

The following examples use two fresh agents to show exactly how each outcome affects scores and confidence. Both start with no interaction history, so scores default to 0.5 and confidence to 0.0.

**Setup**

- **Agent A** — standalone auth, default score = 0.5, ≥ 3 prior interactions (interaction_count_penalty = 1.0), not Sybil-flagged (sybil_multiplier = 1.0)
- **Agent B** — standalone auth, default score = 0.5, ≥ 3 prior interactions (interaction_count_penalty = 1.0), not Sybil-flagged (sybil_multiplier = 1.0)

A calls `report_interaction` with `interaction_type = transaction`. A `transaction` interaction contributes to both `overall` and `reliability` score types; `responsiveness` and `honesty` are unaffected.

### Shared weight calculation (used in all single-report examples)

With both agents having ≥ 3 interactions and no Sybil flags, `interaction_count_penalty = 1.0` and `sybil_multiplier = 1.0`, so the full formula reduces to the base case:

$$\text{credibility} = (0.5 + 0.5 \times 0.5) \times 0.8 \times 1.0 \times 1.0 = 0.75 \times 0.8 = 0.60$$

$$w = \underbrace{1.0}_{\text{age = 0 days}} \times 0.60 \times \underbrace{1.0}_{\text{not confirmed}} = 0.60$$

Because both agents appear in the interaction (A as initiator, B as counterparty), the engine fetches this row when computing **either** agent's score. Both start from the same prior of α = 2, β = 2.

---

### Outcome: `success`

**Agent A (reporter):**

$$\alpha = 2.0 + 0.60 \times 0.5 = 2.30 \quad \beta = 2.0$$

$$\text{score} = \frac{2.30}{2.30 + 2.0} = \frac{2.30}{4.30} \approx 0.5349$$

**Agent B (counterparty):**

$$\alpha = 2.0 + 0.60 = 2.60 \quad \beta = 2.0$$

$$\text{score} = \frac{2.60}{2.60 + 2.0} = \frac{2.60}{4.60} \approx 0.5652$$

$$\text{confidence} = 1 - \frac{1}{1 + 1 \times 0.1} = 1 - \frac{1}{1.1} \approx 0.0909$$

|                         | Before | After  |
| ----------------------- | ------ | ------ |
| Agent A score (overall) | 0.5000 | 0.5349 |
| Agent B score (overall) | 0.5000 | 0.5652 |
| Confidence (both)       | 0.0000 | 0.0909 |

Both agents benefit, but Agent B (who delivered successfully) benefits more than Agent A (who reported the interaction).

---

### Outcome: `failure`

**Agent A (reporter):**

$$\alpha = 2.0 \quad \beta = 2.0$$

$$\text{score} = \frac{2.0}{4.0} = 0.5000$$

**Agent B (counterparty):**

$$\beta = 2.0 + 0.60 = 2.60 \quad \alpha = 2.0$$

$$\text{score} = \frac{2.0}{4.60} \approx 0.4348$$

|                         | Before | After  |
| ----------------------- | ------ | ------ |
| Agent A score (overall) | 0.5000 | 0.5000 |
| Agent B score (overall) | 0.5000 | 0.4348 |
| Confidence (both)       | 0.0000 | 0.0909 |

Only Agent B (counterparty) is penalized for the failure. Agent A's score remains unchanged — they are not penalized for honestly reporting a negative outcome. A `timeout` outcome produces identical numbers for Agent B.

---

### Outcome: `partial`

**Agent A (reporter):**

$$\alpha = 2.0 + 0.60 \times 0.25 = 2.15 \quad \beta = 2.0$$

$$\text{score} = \frac{2.15}{4.15} \approx 0.5181$$

**Agent B (counterparty):**

$$\alpha = 2.0 + 0.60 \times 0.5 = 2.30 \quad \beta = 2.0 + 0.60 \times 0.5 = 2.30$$

$$\text{score} = \frac{2.30}{4.60} = 0.5000$$

|                         | Before | After  |
| ----------------------- | ------ | ------ |
| Agent A score (overall) | 0.5000 | 0.5181 |
| Agent B score (overall) | 0.5000 | 0.5000 |
| Confidence (both)       | 0.0000 | 0.0909 |

Agent A (reporter) receives a small participation boost. Agent B's score stays exactly at the prior mean (neutral outcome with equal α and β increments). Confidence still grows for both.

---

### Mutual confirmation bonus

After A's success report in the first example, B independently files the same transaction as `success`. The engine marks both rows `mutually_confirmed = True`.

At this point A has a score of ≈ 0.5349 (reporter credit) and B has ≈ 0.5652 (counterparty credit), so their credibility as reporters differs slightly.

**Row 1** (reported by A, now mutually confirmed):

$$\text{credibility}_A = (0.5 + 0.5349 \times 0.5) \times 0.8 = 0.7674 \times 0.8 = 0.6139$$

$$w_1 = 1.0 \times 0.6139 \times 1.5 = 0.9209$$

**Row 2** (reported by B, now mutually confirmed):

$$\text{credibility}_B = (0.5 + 0.5652 \times 0.5) \times 0.8 = 0.7826 \times 0.8 = 0.6261$$

$$w_2 = 1.0 \times 0.6261 \times 1.5 = 0.9391$$

Both rows contribute to each agent's score, but the **role matters**:

**Agent A** (reported row 1, counterparty in row 2):

$$\alpha = 2.0 + 0.9209 \times 0.5 + 0.9391 = 2.0 + 0.4605 + 0.9391 = 3.3996 \quad \beta = 2.0$$

$$\text{score} = \frac{3.3996}{5.3996} \approx 0.6296$$

**Agent B** (counterparty in row 1, reported row 2):

$$\alpha = 2.0 + 0.9209 + 0.9391 \times 0.5 = 2.0 + 0.9209 + 0.4696 = 3.3905 \quad \beta = 2.0$$

$$\text{score} = \frac{3.3905}{5.3905} \approx 0.6290$$

$$\text{confidence} = 1 - \frac{1}{1 + 2 \times 0.1} = 1 - \frac{1}{1.2} = 0.1667$$

|                 | Agent A (single) | Agent B (single) | After mutual confirmation (A) | After mutual confirmation (B) |
| --------------- | ---------------- | ---------------- | ----------------------------- | ----------------------------- |
| Score           | 0.5349           | 0.5652           | 0.6296                        | 0.6290                        |
| Confidence      | 0.0909           | 0.0909           | 0.1667                        | 0.1667                        |

The mutual confirmation bonus (1.5×) substantially increases the effective weight. The scores are nearly symmetric but Agent A (who delivered as counterparty in the second report) receives slightly more total credit.

> **Diminishing returns:** The 1.5× bonus applies to the **first** mutually confirmed interaction between a given pair. Subsequent confirmations within the same scoring window are subject to diminishing returns:
> $$\text{mutual\_bonus} = \max\!\bigl(1.5 - 0.1 \times (\text{pair\_count} - 1),\; 1.0\bigr)$$
> where `pair_count` is the number of existing mutually-confirmed interactions between this specific pair. The 2nd confirmation yields 1.4×, the 3rd 1.3×, and the 6th onward is capped at 1.0× (no bonus). The example above shows the first confirmation (pair_count = 1 → bonus = 1.5).

---

### Time decay effect

The same interaction reported 180 days ago (two half-lives), with A as reporter and B as counterparty:

$$\text{time\_weight} = 0.5^{180/90} = 0.5^2 = 0.25$$

$$w = 0.25 \times 0.60 \times 1.0 = 0.15$$

**Agent A (reporter):**

$$\alpha = 2.0 + 0.15 \times 0.5 = 2.075 \quad \text{score} = \frac{2.075}{4.075} \approx 0.5092$$

**Agent B (counterparty):**

$$\alpha = 2.0 + 0.15 = 2.15 \quad \text{score} = \frac{2.15}{4.15} \approx 0.5181$$

A success from six months ago barely moves the needle — both scores are almost back to the 0.5 prior.

---

### High-credibility reporter effect

Same fresh agents, but A is now a `root`-level AgentAuth agent with a score of 0.9. A reports a `success` interaction with B.

$$\text{credibility} = (0.5 + 0.9 \times 0.5) \times 1.2 = 0.95 \times 1.2 = 1.14$$

$$w = 1.0 \times 1.14 \times 1.0 = 1.14$$

**Agent A (reporter):**

$$\alpha = 2.0 + 1.14 \times 0.5 = 2.57 \quad \text{score} = \frac{2.57}{4.57} \approx 0.5623$$

**Agent B (counterparty):**

$$\alpha = 2.0 + 1.14 = 3.14 \quad \text{score} = \frac{3.14}{5.14} \approx 0.6109$$

The same outcome carries nearly twice the weight compared to a neutral standalone reporter (w = 0.60 vs w = 1.14). Agent B benefits significantly from the high-credibility reporter, reaching 0.611 instead of 0.565. Agent A benefits less (0.562 vs 0.535) since reporters receive half credit.

---

### Dispute penalty effect

Agent B accumulates 10 successful interactions giving a raw score of 0.85, but 5 disputes against them are upheld:

$$\text{dispute\_penalty} = \max(1.0 - 5 \times 0.03,\; 0.5) = \max(0.85,\; 0.5) = 0.85$$

$$\text{score} = 0.85 \times 0.85 = 0.7225$$

With 17 upheld disputes (enough to hit the floor):

$$\text{dispute\_penalty} = \max(1.0 - 17 \times 0.03,\; 0.5) = \max(0.49,\; 0.5) = 0.5$$

$$\text{score} = 0.85 \times 0.5 = 0.425$$

No matter how many disputes are lost, the penalty cannot reduce the multiplier below 0.5 — a raw score of 0.85 can never fall below 0.425 through disputes alone.

---

## Score Recomputation

Score recomputation can happen on two paths:

### Asynchronous (background worker)

After `report_interaction` is called or a dispute is resolved, an arq job `recompute_score` is enqueued for **both** the reporter and counterparty. The worker:

1. Reads the current overall score (for change detection).
2. Recomputes all four score types from the full interaction history.
3. Upserts the results in the `trust_scores` table.
4. Invalidates the Redis cache for that agent (`score:{agent_id}:*`).
5. If the overall score changed, enqueues `dispatch_alerts` to notify subscribers.
6. If the overall score dropped by more than 0.10 in this recomputation, any outstanding non-revoked attestations for the agent are **proactively revoked**. This prevents agents from presenting stale high-score attestations after a significant trust event.

### Synchronous (cache miss path)

When `check_trust` is called and no valid cache entry exists (TTL = 60 seconds), the score is recomputed inline from raw interactions and the result is upserted before being returned. This ensures scores are never served from a stale database row.

### Nightly Decay Refresh

A nightly cron job (`refresh_all_scores`, scheduled at 2 AM UTC) recomputes scores for all active agents across all four score types and flushes the full Redis score cache. This ensures time-decay is continuously reflected even for agents with no recent interaction activity.

---

## Factor Breakdown

The full computation details are persisted in the `factor_breakdown` JSONB column of `trust_scores` and can be retrieved via the `get_score_breakdown` MCP tool (requires `trust.read` scope). The breakdown includes:

- `bayesian_raw` — pre-penalty Bayesian mean
- `alpha` / `beta` — final Beta distribution parameters
- `dispute_penalty` / `dismissed_penalty` — applied multipliers
- `interaction_count` — number of interactions included
- Per-interaction weight details

---

## Sybil Detection

The `SybilDetector` runs three independent checks and returns a `risk_score` equal to the highest severity signal found:

| Signal               | Trigger                                    | Severity                              |
| -------------------- | ------------------------------------------ | ------------------------------------- |
| `ring_reporting`     | ≥ 2 mutual positive reports within 30 days | `min(0.3 + (count − 2) × 0.1, 0.9)`   |
| `burst_registration` | ≥ 5 agents registered within ±1 hour       | `min(0.2 + count × 0.04, 0.8)`        |
| `delegation_chain`   | delegation chain depth ≥ 3                 | `min(0.3 + (depth − 3) × 0.15, 0.85)` |

The detector exposes a credibility multiplier that can suppress a suspicious reporter's contribution to scores:

| Risk Level                      | Multiplier |
| ------------------------------- | ---------- |
| High (`risk_score ≥ 0.7`)       | 0.3×       |
| Suspicious (`risk_score ≥ 0.4`) | 0.6×       |
| Clean                           | 1.0×       |

> The Sybil multiplier is computed via `get_sybil_credibility_multiplier()` in `sybil_detector.py` and is **actively applied** inside `ScoreComputation.compute()` for every reporter. Results are cached per-reporter within a single recomputation to avoid redundant database queries.

---

## Configuration Reference

| Environment Variable    | Default | Description                         |
| ----------------------- | ------- | ----------------------------------- |
| `SCORE_HALF_LIFE_DAYS`  | `90`    | Exponential decay half-life in days |
| `DISPUTE_PENALTY`       | `0.03`  | Penalty per lost dispute            |
| `ATTESTATION_TTL_HOURS` | `12`    | Default attestation validity window |

The `DISPUTE_PENALTY` value maps directly to `dispute_penalty_per` in `ScoreComputation`. Floor values and the dismissed-penalty rate are hardcoded constants in `score_engine.py`.
