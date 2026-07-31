# Deployment

AgentTrust runs on the iMac, not on a VPS. It was migrated off Hetzner on
2026-07-31, following the same pattern as agent-auth.

```
internet → Cloudflare (TLS) → Cloudflare Tunnel → 127.0.0.1:8140 → app container
```

There is no nginx, no Caddy, no reverse proxy of any kind. cloudflared runs on the
host and maps the hostname straight to the app's loopback port.

| | |
| --- | --- |
| Public URL | `https://agent-trust.radi.pro/mcp` |
| Local port | `127.0.0.1:8140` |
| Compose project | `agent-trust` (`docker-compose.prod.yml`) |
| Tunnel | `agentauth-imac` (shared with agentauth.radi.pro and logs.radi.pro) |
| Dashboards | `https://logs.radi.pro` → folder `agent-trust` |
| Container runtime | Docker via Colima |

## Deploying

```bash
make deploy                  # pull, build, migrate, restart, health-check
./scripts/deploy.sh --no-pull   # same, but deploy the working tree as-is
```

The script builds the image, runs `alembic upgrade head`, recreates the containers
and then polls `http://127.0.0.1:8140/health` until it returns 200. If it never
does, the deploy fails loudly.

Restarting drops requests for a few seconds; the tunnel retries through the gap.
There is no blue/green — a zero-downtime slot is not worth its weight on a single
box that both develops and serves.

**Rollback:**

```bash
git checkout <last-good-sha> && ./scripts/deploy.sh --no-pull
```

Note that this is fix-forward territory: by the time a health check fails, the old
container is already gone.

## Prerequisites

- **Colima** running (`colima status`), started by `brew services start colima`.
- **docker-buildx** (`brew install docker-buildx`). The Dockerfile uses
  `RUN --mount=type=cache`, which Colima's default classic builder cannot parse —
  it fails with a bare "requires BuildKit". `scripts/deploy.sh` and the Makefile
  both export `DOCKER_BUILDKIT=1`.
- The **`observability` Docker network**: `docker network create observability`.
  The app joins it so the shared Prometheus can scrape it.
- **`.env.prod`** in the repo root, and **`keys/service.key`**. Both are gitignored
  and live only on this machine — see below.

## Secrets

`.env.prod`, not `.env`. This matters: `pydantic-settings` and the test suite both
read `.env`, so production credentials in that file make `pytest` run against
production. Compose needs the file named in two places, because they are different
mechanisms:

- `--env-file .env.prod` — feeds `${VAR}` interpolation in the compose file
- `env_file: .env.prod` — feeds the environment inside the containers

`DATABASE_URL` and `REDIS_URL` are set in the compose file's `environment:` block,
which overrides anything `.env.prod` carries: containers talk to service names, not
to published dev ports.

### The signing key

`keys/service.key` is an Ed25519 private key that signs every attestation JWT. It
is **copied** between machines, never regenerated. A new key silently invalidates
every attestation ever issued — nothing errors, verification just starts failing.

Do not run `scripts/generate_keypair.py` on a host that already has a key.

To prove a copied key is intact, verify a stored attestation JWT against the public
key rather than trusting a checksum:

```bash
docker compose --env-file .env.prod -f docker-compose.prod.yml \
  exec -T postgres psql -U agent_trust -d agent_trust \
  -Atc "SELECT jwt_token FROM attestations LIMIT 1" > /tmp/att.jwt

uv run python -c "
import jwt, pathlib
tok = pathlib.Path('/tmp/att.jwt').read_text().strip()
jwt.decode(tok, pathlib.Path('keys/service.pub').read_text(), algorithms=['EdDSA'],
           options={'verify_exp': False, 'verify_aud': False})
print('signature verifies')"
```

`verify_exp` is off deliberately — whether an old attestation has expired says
nothing about whether the key survived the move.

## The stacks

Two compose projects, deliberately named so neither treats the other's containers
as orphans:

| File | Project | Purpose |
| --- | --- | --- |
| `docker-compose.prod.yml` | `agent-trust` | serving. Postgres/Redis unpublished, app on `127.0.0.1:8140` |
| `docker-compose.yml` | `agent-trust-dev` | development. Postgres `127.0.0.1:5433`, Redis `127.0.0.1:6380` |

Scripts that need to find a container should match on the
`com.docker.compose.project` label, never on the container name.

Only the `agent-trust` service has `build:`; `worker` reuses the
`agent-trust:latest` tag it produces. Two services building the same context to
the same tag race and fail with `AlreadyExists`.

## Observability

Nothing in this repo ships logs or metrics anywhere. The shared stack in
`~/Projects/observability` runs one Grafana, one Prometheus and one Loki for every
service on the machine.

- **Logs** need no configuration at all — Alloy discovers every container on the
  Docker socket. Query them with `{compose_project="agent-trust"}`.
- **Metrics** are scraped from `agent-trust:8000/metrics/` — the target is declared
  in `~/Projects/observability/prometheus/prometheus.yml`, and the app joins the
  `observability` network so the name resolves. The trailing slash is load-bearing:
  the bare `/metrics` 307-redirects.
- **The dashboard** stays versioned here, in `monitoring/grafana/dashboards/`, and
  is bind-mounted read-only into the shared Grafana. Every panel pins
  `datasource: {type: prometheus, uid: prometheus}` — Loki is the default
  datasource there, so an unpinned panel silently queries Loki with PromQL.

There is no `logs-agent-trust.radi.pro` any more; it was retired with this
project's own Grafana.

## Database

PostgreSQL 16 with **TimescaleDB** — `interactions` is a hypertable, so a plain
`pg_restore` produces a broken schema. The image is pinned
(`timescale/timescaledb:2.25.2-pg16`) rather than tracking `latest-pg16`, so a
restore never lands on a version that has drifted ahead of the dump.

Restoring a dump:

```bash
pc() { docker compose --env-file .env.prod -f docker-compose.prod.yml "$@"; }

pc up -d postgres
pc exec -T postgres psql -U agent_trust -d agent_trust -c "SELECT timescaledb_pre_restore();"
pc exec -T postgres pg_restore -U agent_trust -d agent_trust --no-owner --no-privileges < dump.dump
pc exec -T postgres psql -U agent_trust -d agent_trust -c "SELECT timescaledb_post_restore();"
```

`pg_dump` warns about circular foreign keys on `hypertable`, `chunk` and
`continuous_agg`. Those are TimescaleDB's internal catalog tables and are exactly
what `timescaledb_pre_restore()` exists to handle — the warnings are expected.

Afterwards, confirm the hypertable actually survived:

```sql
SELECT hypertable_name, num_chunks FROM timescaledb_information.hypertables;
```

`POSTGRES_PASSWORD` is only honoured when Postgres first initialises a volume.
Pointing an existing volume at a new password fails with `InvalidPasswordError`;
the fix is `down -v`, re-init, and restore again.

## Health

`GET /health` returns the same checks as the `trust://health` MCP resource —
database, Redis, and AgentAuth reachability — with HTTP 200 when everything is
`ok` and 503 otherwise. It exists so the deploy script can tell a container that
started from a container that works.

```bash
curl -s localhost:8140/health | jq
```

## Known weakness: unattended reboot

FileVault is on, so automatic login is impossible, and Colima's LaunchAgent needs
a login session. After an unattended reboot every container stays down until
someone unlocks the disk. `cloudflared` is a LaunchDaemon and comes back on its
own, so it serves 502s in the meantime.

`sudo pmset -a autorestart 1` helps with power-loss restarts but does not solve
the FileVault half.
