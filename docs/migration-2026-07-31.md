# Migration log: Hetzner → iMac (2026-07-31)

Record of how AgentTrust was moved off the Hetzner VPS and onto the iMac, following
the pattern already established for agent-auth earlier the same day. For the
resulting steady-state setup, see [deployment.md](deployment.md). For the reusable
procedure, see `~/.claude/memory/imac-service-migration-playbook.md`.

## Before

| | Hetzner | iMac |
| --- | --- | --- |
| app | `0.0.0.0:8140` | `127.0.0.1:8140` |
| postgres | `127.0.0.1:5433`, timescaledb 2.25.2 / PG 16.11 | not published |
| redis | `127.0.0.1:6380` | not published |
| prometheus | own, `127.0.0.1:9090` | shared stack |
| grafana | own, `0.0.0.0:3001`, `logs-agent-trust.radi.pro` | shared, `logs.radi.pro` |
| signing key | `keys/service.key`, bind-mounted | copied verbatim |

Data at cutover: 7 agents, 28 trust scores, 3 attestations, 8 interactions — 10 MB.

## 1. Code and config changes

Done first, on the working tree, with the old service still serving from Hetzner.

- **`src/agent_trust/server.py`** — added a `/health` route next to the existing
  `/metrics` mount. It wraps the existing `trust://health` MCP resource
  (`get_health()`) and returns HTTP 200 when database, Redis and AgentAuth are all
  `ok`, 503 otherwise. Needed because the deploy script has nothing else to poll —
  health previously existed only as an MCP resource.
- **`docker-compose.prod.yml`** (new) — the serving stack, modelled on
  agent-auth's. Compose project named `agent-trust`; Postgres pinned to
  `timescale/timescaledb:2.25.2-pg16` (not `latest-pg16`, so a future restore
  can't land on a version ahead of the dump) and Redis unpublished; the app on
  `127.0.0.1:8140`; only the app service has `build:`, the worker reuses its
  `agent-trust:latest` tag; both join the external `observability` network.
  No Prometheus or Grafana in this file.
- **`docker-compose.yml`** — added `name: agent-trust-dev` so the dev and prod
  compose projects never treat each other's containers as orphans. Removed the
  `prometheus` and `grafana` services and their volumes.
- **`scripts/deploy.sh`** (new) — copied from agent-auth's and re-pointed at
  `docker-compose.prod.yml`, `.env.prod`, and `http://127.0.0.1:8140/health`.
  Builds, migrates, restarts, then polls health before declaring success.
- **`Makefile`** — added `deploy`, `prod-up`, `prod-down`, `prod-logs`, `prod-ps`.
- **`~/Projects/observability/docker-compose.yml`** — added a `prometheus`
  service (30-day retention) on the `observability` network, since agent-trust
  is the first service on this machine to expose metrics. Added a Prometheus
  datasource (`uid: prometheus`, not default — Loki is) and a dashboard provider
  for `agent-trust`, mounting `~/Projects/agent-trust/monitoring/grafana/dashboards`
  read-only.
- **`monitoring/`** — moved `agent_trust.json` into
  `monitoring/grafana/dashboards/` and rewrote every panel and target to pin
  `datasource: {type: prometheus, uid: prometheus}`. Without this the dashboard
  would have silently queried Loki (the shared stack's default datasource) and
  rendered nothing. Deleted the old local Prometheus/Grafana provisioning.
- **`.github/workflows/deploy.yml`** — deleted. It SSH'd into Hetzner on every
  green build; there is no CD any more, only `make deploy` run by hand.
- **`.gitignore`** — added `.env.prod` and `backups/` explicitly. The existing
  `.env` rule does not match `.env.prod`, which left production secrets one
  `git add` away from being committed.
- **Docs** — rewrote the CI/CD and deployment sections of `DEVELOP.md` and
  `PROJECT.md`, the Docker section of `README.md`, and wrote
  [deployment.md](deployment.md) as the operational guide.

Committed as `d7eaffa` (agent-trust) and `4665c28` (observability, its first
commit).

## 2. Secrets and signing key

Copied from Hetzner, then verified byte-for-byte rather than eyeballed:

```bash
scp hetzner:/home/admin/agent-trust/.env                                  ./.env.prod
scp hetzner:/home/admin/agent-trust/keys/service.{key,pub}                ./keys/
shasum -a 256 keys/service.key .env.prod
ssh hetzner 'shasum -a 256 /home/admin/agent-trust/keys/service.key /home/admin/agent-trust/.env'
```

Both checksums matched. Dropped `GRAFANA_PASSWORD` from `.env.prod` (no local
Grafana any more). The signing key matters most here: it's an Ed25519 key that
signs every attestation JWT, and a different key silently invalidates every
attestation ever issued with no error at read time.

## 3. Dry run

Before touching Hetzner's live containers:

1. `pg_dump -Fc --no-owner` from the still-running Hetzner Postgres — read-only,
   no impact on the live service.
2. Captured baselines: row counts per table, and every `(agent_id, score)` pair
   in `trust_scores`.
3. Built the image locally. Hit an immediate blocker: Colima's docker daemon
   defaults to the classic builder, which can't parse the Dockerfile's
   `RUN --mount=type=cache` and fails with a bare "requires BuildKit" error.
   Fixed by `brew install docker-buildx` (the plugin directory was already
   registered in `~/.docker/config.json`) and exporting `DOCKER_BUILDKIT=1` —
   now baked into `deploy.sh` and the `Makefile`.
4. Restored into a fresh volume with the TimescaleDB dance a plain `pg_restore`
   can't do on its own:
   ```bash
   psql -c "SELECT timescaledb_pre_restore();"
   pg_restore --no-owner --no-privileges < dump.dump
   psql -c "SELECT timescaledb_post_restore();"
   ```
   Zero errors. Row counts and trust scores matched the baseline exactly; the
   `interactions` hypertable came back with its chunk intact.
5. Brought up the full stack and confirmed `/health` returned `healthy`, then
   confirmed the copied signing key was live, not just byte-identical: decoded
   one of the three stored attestation JWTs against `keys/service.pub` with
   `verify_exp=False` (all three were already revoked and expired, so
   `verify_attestation` itself couldn't prove this) — it verified.
6. Tore the dry run down (`down -v`) and discarded it.

## 4. Cutover

1. **Freeze writes on Hetzner** — stopped only `agent-trust` and `worker`:
   ```bash
   ssh hetzner 'cd /home/admin/agent-trust && docker compose stop agent-trust worker'
   ```
   Confirmed nothing else on that box (agent-auth's leftover containers,
   fotoatelie, Plausible, filebrowser, etc.) was touched.
2. **Final dump** from the now-frozen database, plus refreshed baselines.
3. **Restore locally** using the same pre/post_restore sequence as the dry run —
   clean, no errors.
4. **`./scripts/deploy.sh --no-pull`** — built the image, ran
   `alembic upgrade head`, brought up the full stack, and polled `/health` until
   it returned 200 (8 seconds). Verified the restored data against the frozen
   baseline again post-deploy: exact match.
5. **Attached the Public Hostname** — done manually via the Cloudflare Zero
   Trust dashboard (Networks → Tunnels → `agentauth-imac` → Public Hostname),
   since there are no Cloudflare API credentials on this machine. This is the
   moment the outage ended; everything before it was reversible with Hetzner
   still serving.

## 5. Verification, from outside

All against `https://agent-trust.radi.pro`, not localhost:

- `/health` → 200, all three checks `ok`.
- MCP `list_tools` → all 23 tools present; `check_trust` returned real data for
  a migrated agent.
- **Confirmed the iMac, not Hetzner, was actually serving it** — not by log
  grep (agent-trust logs no access lines, so the usual unique-User-Agent trick
  doesn't apply here) but by watching the live Prometheus counter
  `agent_trust_tool_calls_total{tool_name="check_trust"}` climb from 1 to 4 as
  calls were made through the public URL.
- Full write + auth round trip: `register_agent` over the public endpoint,
  confirmed the row landed in Postgres, minted a signed token with
  `generate_agent_token`, and called `whoami` with it successfully. Deleted the
  throwaway agent afterward.
- AgentAuth integration: session established and `introspect_token` answered
  (visible in the app's structured logs), proving the cross-service MCP call
  still works with both services co-located.
- Data integrity re-checked one more time post-verification: row counts and
  trust scores matched the frozen baseline exactly except `trust_scores.overall`
  for the one agent used in testing — expected, since AgentTrust recomputes that
  score on read; the cron-written `honesty`/`reliability`/`responsiveness` rows
  (timestamped 02:00, untouched by any read) were still byte-identical.
- Prometheus target `agent-trust:8000/metrics/` — `up`.
- Dashboard rendering in the `agent-trust` folder at `logs.radi.pro`.
- Logs for all four containers (`agent-trust`, `postgres`, `redis`, `worker`)
  flowing into Loki under `compose_project="agent-trust"`.
- `246 passed` on the test suite (in-memory SQLite, unaffected by any of this).

## Known issues found, not yet resolved

- **`AGENTAUTH_ACCESS_TOKEN` in `.env.prod` is expired.** Copied byte-identically
  from Hetzner, so this predates the migration. It's the Bearer token AgentTrust
  uses for its own MCP session to AgentAuth — the session still establishes and
  introspection still answers today, but it should be reissued before it
  matters.
- **Hetzner cleanup is still open**: `agent-trust.radi.pro` and
  `logs-agent-trust.radi.pro` have not yet been removed from the Hetzner
  tunnel, and agent-trust's Postgres/Redis/Grafana/Prometheus containers there
  are still running (app and worker only were stopped, to keep a rollback path).
- **`~/.ssh/agentauth_migration_ed25519`** has now outlived both migrations
  (agent-auth and agent-trust) and should be revoked from Hetzner's
  `authorized_keys` and deleted at both ends.
- **Unattended-reboot weakness carries over**: FileVault blocks automatic login,
  so Colima's LaunchAgent — and therefore every container, including
  agent-trust — stays down after a reboot until someone unlocks the disk.
  `cloudflared` comes back on its own and serves 502s in the meantime.
