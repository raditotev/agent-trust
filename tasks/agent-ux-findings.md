# AgentTrust — Agent UX Findings & Backlog

Collected during coordinated two-phase integration test (2026-03-22) between
Alpha (`730e71f5-...`) and Beta (`d8a24cef-...`), covering `link_agentauth`,
`issue_attestation`, `verify_attestation`, and the mutual-confirmation flow.

---

## Bugs Fixed (committed)

| Commit    | File                            | Description                                                                                                                                                                                                                                                          |
| --------- | ------------------------------- | -------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `33367db` | `auth/agentauth.py`             | `list(None)` crash when `introspect_token` returns `"scopes": null` — `.get("key", default)` ignores the default when the key exists with value `None`                                                                                                               |
| `cca1dbb` | `auth/agentauth.py`             | AgentAuth tokens carry `scopes: []` from AgentAuth's namespace; AgentTrust's own scopes (`trust.attest.issue` etc.) were never granted — added `AGENTAUTH_DEFAULT_SCOPES`                                                                                            |
| `cca1dbb` | `auth/resolve.py`               | After `link_agentauth`, authenticating with the AgentAuth token (sub: `069bfede-...`) resolved to an empty "profile not yet created" response instead of the linked standalone profile (`730e71f5-...`) — added reverse JSONB lookup on `metadata_["agentauth_id"]`  |
| `fdcaf04` | `tools/interactions.py`         | Auto-match query for mutual confirmation had no `mutually_confirmed == False` guard — any old confirmed Beta→Alpha interaction from a prior session would immediately mark a new Alpha report as `mutually_confirmed: true`, skipping the confirmation flow entirely |
| `2fc876c` | `config.py` + `interactions.py` | 1-hour dedup window was hardcoded, blocking repeated integration tests — extracted to `INTERACTION_DEDUP_WINDOW_SECONDS` setting (default 3600, set to 0 to disable)                                                                                                 |

---

## UX / DX Issues to Address

### 1. `link_agentauth` has too many failure modes with poor diagnostics

**Problem:** The call requires four things to be correct simultaneously: a fresh AgentAuth
token, a correctly derived public key hex, a signed proof JWT with exact claims (`sub`,
`action`, `iat`), and the `iat` within a 300-second window. Any mistake produces an opaque
error. Stale `iat` in particular fails silently when tests are slow.

**Suggestions:**

- Add a `verify_link_proof` preflight tool (or a dry-run parameter) that validates the
  signed proof without committing the link, so agents can confirm their proof is correct
  before the irreversible operation.
- Return a specific error code for each failure mode (`proof_expired`, `proof_sig_invalid`,
  `key_not_found`, `already_linked`) rather than a generic auth error.

---

### 2. Silent empty profile after `link_agentauth` (fixed, but design needs hardening)

**Problem:** After linking, any tool that called `resolve_identity` would get
`agent_id = "069bfede-..."` (the AgentAuth sub), look it up in the DB, find nothing,
and silently return an empty "profile not yet created" response. No error — just wrong data.

**Suggestions:**

- The reverse JSONB lookup fix (`resolve.py`) handles this at runtime, but the lookup
  runs on every authenticated call. Consider caching the `agentauth_id → canonical_id`
  mapping in Redis (keyed by AgentAuth sub, TTL matching token expiry) to avoid a DB
  hit per request.
- Alternatively, at link time, write the AgentAuth UUID as a proper FK or secondary index
  column rather than burying it in `metadata_` JSONB — faster lookups and explicit schema.

---

### 3. No single "agent health check" call

**Problem:** To get a complete picture of current state, an agent must chain:

1. `whoami` — scores, scopes, agentauth_linked
2. `list_pending_confirmations` — interactions awaiting confirmation
3. (remember attestation IDs externally) — no way to query own active attestations

**Suggestion:** Add an `agent_status` tool that returns in one call:

```json
{
  "agent_id": "...",
  "agentauth_linked": true,
  "scores": { "overall": 0.73, "reliability": 0.73 },
  "scopes": ["trust.read", "trust.report", "trust.attest.issue"],
  "pending_confirmations": 2,
  "active_attestations": [
    {
      "attestation_id": "...",
      "valid_until": "...",
      "seconds_remaining": 43000
    }
  ]
}
```

---

### 4. `whoami` returns `agent_id` of the AgentAuth UUID, not canonical UUID

**Problem (pre-fix):** After `link_agentauth`, `whoami` with the AgentAuth token was
returning `agent_id: "069bfede-..."` (the AgentAuth sub) in the "profile not yet created"
path. Even with the fix, it's worth documenting the expected canonical ID contract:
after linking, the canonical `agent_id` should always be the original standalone UUID
(`730e71f5-...`), and `agentauth_id` in metadata is the foreign reference.

**Suggestion:** Document this contract explicitly in the `link_agentauth` and `whoami`
docstrings. Add a `canonical_agent_id` field to the `link_agentauth` response so callers
know which UUID to use going forward.

---

### 5. `link_agentauth` is irreversible with no preview of what transfers

**Problem:** The operation is one-way and transfers interaction history + scores. An agent
has no way to preview what will be merged before committing.

**Suggestion:** Add a `dry_run=True` parameter that returns what _would_ transfer
(score snapshot, interaction count, capabilities) without mutating anything.

---

## Feature Requests

### F1. `agent_status` tool (see UX issue #3 above)

### F2. Attestation query for self

Allow an authenticated agent to list their own active (non-revoked, non-expired)
attestations: `list_my_attestations(access_token)`. Currently there is no way to retrieve
your own attestation JWTs after the fact — agents must store them externally at issuance time.

### F3. `verify_link_proof` preflight

Validate a `signed_proof` JWT against a `public_key_hex` without committing the link.
Useful for debugging before the irreversible `link_agentauth` call.

### F4. `agentauth_id → canonical_id` Redis cache

After the reverse JSONB lookup is performed in `resolve_identity`, cache the result in
Redis with a short TTL (e.g., 5 minutes) so subsequent calls don't hit the DB.
Key: `agentauth_id_map:{agentauth_uuid}` → `canonical_uuid`.

### F5. Structured `link_agentauth` error codes

Replace the current single `AuthenticationError` with specific codes:

- `proof_expired` — `iat` outside 300-second window
- `proof_sig_invalid` — JWT signature failed
- `key_not_found` — no standalone agent with that `public_key_hex`
- `already_linked` — standalone profile already has `auth_source = "agentauth"`
