from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import structlog
from sqlalchemy import func, select

from agent_trust.auth.identity import AuthenticationError, AuthorizationError
from agent_trust.auth.provider import require_scope
from agent_trust.db.session import get_session
from agent_trust.models import Agent, Interaction

log = structlog.get_logger()

VALID_INTERACTION_TYPES = {"transaction", "delegation", "query", "collaboration"}
VALID_OUTCOMES = {"success", "failure", "timeout", "partial"}


async def _resolve_identity_for_interaction(access_token: str | None):
    """Resolve and validate agent identity for interaction reporting.

    Delegates to the shared resolver which handles AgentAuth tokens,
    standalone signed JWTs, and (legacy) public key lookups.
    """
    from agent_trust.auth.resolve import resolve_identity

    return await resolve_identity(access_token=access_token)


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

    interaction_type options: transaction | delegation | query | collaboration
    outcome options: success | failure | timeout | partial
    context: optional dict with amount, task_type, duration_ms, sla_met
    evidence_hash: optional SHA-256 hash of supporting evidence

    Authentication via access_token:
    - AgentAuth token: obtain from agentauth.radi.pro
    - Standalone signed JWT: generate with scripts/generate_agent_token.py
      using your agent's private_key_hex from registration

    Returns interaction_id and whether the counterparty has also
    reported on this interaction (mutually_confirmed).

    Requires trust.report scope.
    """
    try:
        identity = await _resolve_identity_for_interaction(access_token)
        require_scope(identity, "trust.report")
    except (AuthenticationError, AuthorizationError) as e:
        return {"error": str(e)}

    from agent_trust.ratelimit import check_rate_limit

    rl_result = await check_rate_limit(
        agent_id=identity.agent_id,
        tool_name="report_interaction",
        trust_level=identity.trust_level,
    )
    if not rl_result.allowed:
        return {
            "error": "Rate limit exceeded",
            "retry_after_seconds": rl_result.retry_after,
        }

    if interaction_type not in VALID_INTERACTION_TYPES:
        return {
            "error": f"Invalid interaction_type. Must be one of: {sorted(VALID_INTERACTION_TYPES)}"
        }
    if outcome not in VALID_OUTCOMES:
        return {"error": f"Invalid outcome. Must be one of: {sorted(VALID_OUTCOMES)}"}

    # Change 3 — Fix #12: Context and evidence_hash size/format validation
    import json as _json
    if context is not None:
        try:
            context_size = len(_json.dumps(context).encode("utf-8"))
        except (TypeError, ValueError):
            return {"error": "context must be a JSON-serializable object"}
        if context_size > 10240:
            return {"error": f"context payload too large: {context_size} bytes (max 10240)"}

    if evidence_hash is not None:
        import re
        if not re.fullmatch(r"[0-9a-fA-F]{64}", evidence_hash):
            return {"error": "evidence_hash must be a valid SHA-256 hex string (64 hex characters)"}

    try:
        reporter_uuid = uuid.UUID(identity.agent_id)
        counterparty_uuid = uuid.UUID(counterparty_id)
    except ValueError as e:
        return {"error": f"Invalid UUID: {e}"}

    if reporter_uuid == counterparty_uuid:
        return {"error": "Cannot report an interaction with yourself"}

    async with get_session() as session:
        reporter_result = await session.execute(
            select(Agent).where(Agent.agent_id == reporter_uuid)
        )
        reporter_agent = reporter_result.scalar_one_or_none()
        if not reporter_agent:
            return {
                "error": f"Your agent profile not found (id={identity.agent_id}). Register first."
            }

        counterparty_result = await session.execute(
            select(Agent).where(Agent.agent_id == counterparty_uuid)
        )
        counterparty_agent = counterparty_result.scalar_one_or_none()
        if not counterparty_agent:
            return {"error": f"Counterparty agent not found (id={counterparty_id})"}

        # Change 1 — Fix #7: Per-pair daily interaction cap
        pair_cutoff = datetime.now(UTC) - timedelta(hours=24)
        pair_count_result = await session.execute(
            select(func.count()).select_from(Interaction).where(
                Interaction.reported_at >= pair_cutoff,
                (
                    (Interaction.initiator_id == reporter_uuid)
                    & (Interaction.counterparty_id == counterparty_uuid)
                )
                | (
                    (Interaction.initiator_id == counterparty_uuid)
                    & (Interaction.counterparty_id == reporter_uuid)
                ),
            )
        )
        pair_count = pair_count_result.scalar() or 0
        if pair_count >= 10:
            return {
                "error": (
                    "Per-pair daily limit reached: maximum 10 interactions "
                    "per agent pair per 24 hours"
                ),
                "pair_interaction_count": pair_count,
            }

        # Change 2 — Fix #10: Duplicate interaction deduplication window
        dedup_cutoff = datetime.now(UTC) - timedelta(hours=1)
        dedup_result = await session.execute(
            select(Interaction).where(
                Interaction.reported_by == reporter_uuid,
                Interaction.counterparty_id == counterparty_uuid,
                Interaction.interaction_type == interaction_type,
                Interaction.reported_at >= dedup_cutoff,
            )
        )
        if dedup_result.scalar_one_or_none():
            return {
                "error": (
                    "Duplicate interaction: same interaction type with this "
                    "counterparty already reported within the last hour"
                )
            }

        # Check if counterparty already reported this interaction (for mutual confirmation)
        existing_result = await session.execute(
            select(Interaction).where(
                Interaction.initiator_id == counterparty_uuid,
                Interaction.counterparty_id == reporter_uuid,
                Interaction.interaction_type == interaction_type,
                Interaction.reported_by == counterparty_uuid,
            )
        )
        counterparty_report = existing_result.scalar_one_or_none()
        mutually_confirmed = counterparty_report is not None

        interaction = Interaction(
            interaction_id=uuid.uuid4(),
            initiator_id=reporter_uuid,
            counterparty_id=counterparty_uuid,
            interaction_type=interaction_type,
            outcome=outcome,
            context=context or {},
            evidence_hash=evidence_hash,
            reported_by=reporter_uuid,
            mutually_confirmed=mutually_confirmed,
            reported_at=datetime.now(UTC),
        )
        session.add(interaction)

        if mutually_confirmed and counterparty_report:
            counterparty_report.mutually_confirmed = True

        await session.flush()

        log.info(
            "interaction_reported",
            interaction_id=str(interaction.interaction_id),
            reporter=identity.agent_id,
            counterparty=counterparty_id,
            outcome=outcome,
            mutually_confirmed=mutually_confirmed,
        )

        try:
            await _enqueue_score_recomputation(reporter_uuid, counterparty_uuid)
        except Exception as e:
            log.warning("score_recompute_enqueue_failed", error=str(e))

        return {
            "interaction_id": str(interaction.interaction_id),
            "reporter_id": identity.agent_id,
            "counterparty_id": counterparty_id,
            "outcome": outcome,
            "mutually_confirmed": mutually_confirmed,
            "reported_at": interaction.reported_at.isoformat(),
        }


async def _enqueue_score_recomputation(
    agent_id_1: uuid.UUID,
    agent_id_2: uuid.UUID,
) -> None:
    """Enqueue background score recomputation for both agents."""
    try:
        import arq

        from agent_trust.config import settings  # noqa: PLC0415

        redis_pool = await arq.create_pool(
            arq.connections.RedisSettings.from_dsn(settings.redis_url)
        )
        await redis_pool.enqueue_job("recompute_score", str(agent_id_1))
        await redis_pool.enqueue_job("recompute_score", str(agent_id_2))
        await redis_pool.aclose()
    except Exception as e:
        log.warning("arq_enqueue_failed", error=str(e))


async def get_interaction_history(
    agent_id: str,
    interaction_type: str | None = None,
    outcome: str | None = None,
    since_days: int = 90,
    limit: int = 50,
    access_token: str | None = None,
) -> dict:
    """Retrieve interaction history for an agent.

    Filter by interaction type and outcome. Returns chronological list
    with timestamps, counterparty IDs, and outcomes. Useful for due
    diligence before high-value transactions.

    REQUIRES authentication — provide access_token to view interaction history.

    since_days: how far back to look (default 90, max 365)
    limit: max results to return (default 50, max 200)
    """
    since_days = min(max(1, since_days), 365)
    limit = min(max(1, limit), 200)

    # Change 4 — Fix #13: Require authentication for get_interaction_history
    if not access_token:
        return {
            "error": (
                "Authentication required: provide access_token to view "
                "interaction history"
            )
        }

    try:
        from agent_trust.auth.resolve import resolve_identity
        await resolve_identity(access_token=access_token)
    except Exception as e:
        return {"error": f"Authentication failed: {e}"}

    try:
        target_uuid = uuid.UUID(agent_id)
    except ValueError:
        return {"error": f"Invalid agent_id UUID: {agent_id}"}

    cutoff = datetime.now(UTC) - timedelta(days=since_days)

    async with get_session() as session:
        agent_result = await session.execute(select(Agent).where(Agent.agent_id == target_uuid))
        if not agent_result.scalar_one_or_none():
            return {"error": f"Agent not found: {agent_id}"}

        query = select(Interaction).where(
            (Interaction.initiator_id == target_uuid)
            | (Interaction.counterparty_id == target_uuid),
            Interaction.reported_at >= cutoff,
        )

        if interaction_type:
            if interaction_type not in VALID_INTERACTION_TYPES:
                return {"error": f"Invalid interaction_type: {interaction_type}"}
            query = query.where(Interaction.interaction_type == interaction_type)

        if outcome:
            if outcome not in VALID_OUTCOMES:
                return {"error": f"Invalid outcome: {outcome}"}
            query = query.where(Interaction.outcome == outcome)

        query = query.order_by(Interaction.reported_at.desc()).limit(limit)
        result = await session.execute(query)
        interactions = result.scalars().all()

        items = []
        for ix in interactions:
            role = "initiator" if ix.initiator_id == target_uuid else "counterparty"
            counterparty = ix.counterparty_id if role == "initiator" else ix.initiator_id
            items.append(
                {
                    "interaction_id": str(ix.interaction_id),
                    "role": role,
                    "counterparty_id": str(counterparty),
                    "interaction_type": ix.interaction_type,
                    "outcome": ix.outcome,
                    "mutually_confirmed": ix.mutually_confirmed,
                    "reported_at": ix.reported_at.isoformat(),
                }
            )

        return {
            "agent_id": agent_id,
            "interactions": items,
            "count": len(items),
            "since_days": since_days,
        }
