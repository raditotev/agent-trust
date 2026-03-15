from __future__ import annotations

import uuid
from datetime import UTC, datetime

import structlog
from sqlalchemy import select

from agent_trust.auth.agentauth import AgentAuthProvider
from agent_trust.auth.identity import AuthenticationError, AuthorizationError
from agent_trust.auth.provider import require_scope
from agent_trust.config import settings
from agent_trust.db.redis import get_redis
from agent_trust.db.session import get_session
from agent_trust.models import Dispute, Interaction

log = structlog.get_logger()

VALID_RESOLUTIONS = {"upheld", "dismissed", "split"}


async def _get_agentauth_provider() -> AgentAuthProvider:
    redis = await get_redis()
    return AgentAuthProvider(redis_client=redis)


async def file_dispute(
    interaction_id: str,
    reason: str,
    access_token: str,
    evidence: dict | None = None,
) -> dict:
    """File a dispute against an interaction outcome.

    Provide the interaction_id from a previously reported interaction
    and a clear reason explaining why you believe the outcome was
    incorrectly reported.

    REQUIRES authentication (access_token) and trust.dispute.file scope.

    Filing frivolous disputes damages your own trust score —
    dismissed disputes apply a small penalty to the filer.

    Returns dispute_id and status ('open').
    """
    provider = await _get_agentauth_provider()
    try:
        identity = await provider.authenticate(access_token=access_token)
        require_scope(identity, "trust.dispute.file")
    except (AuthenticationError, AuthorizationError) as e:
        return {"error": str(e)}

    try:
        interaction_uuid = uuid.UUID(interaction_id)
        filer_uuid = uuid.UUID(identity.agent_id)
    except ValueError as e:
        return {"error": f"Invalid UUID: {e}"}

    async with get_session() as session:
        ix_result = await session.execute(
            select(Interaction).where(Interaction.interaction_id == interaction_uuid)
        )
        interaction = ix_result.scalar_one_or_none()
        if not interaction:
            return {"error": f"Interaction not found: {interaction_id}"}

        if filer_uuid not in (interaction.initiator_id, interaction.counterparty_id):
            return {"error": "You can only file disputes for interactions you were party to"}

        filed_against_uuid = (
            interaction.counterparty_id
            if filer_uuid == interaction.initiator_id
            else interaction.initiator_id
        )

        existing_result = await session.execute(
            select(Dispute).where(
                Dispute.interaction_id == interaction_uuid,
                Dispute.filed_by == filer_uuid,
                Dispute.status == "open",
            )
        )
        if existing_result.scalar_one_or_none():
            return {"error": "You already have an open dispute for this interaction"}

        dispute = Dispute(
            dispute_id=uuid.uuid4(),
            interaction_id=interaction_uuid,
            filed_by=filer_uuid,
            filed_against=filed_against_uuid,
            reason=reason,
            evidence=evidence or {},
            status="open",
            created_at=datetime.now(UTC),
        )
        session.add(dispute)
        await session.flush()

        log.info(
            "dispute_filed",
            dispute_id=str(dispute.dispute_id),
            filed_by=identity.agent_id,
            filed_against=str(filed_against_uuid),
            interaction_id=interaction_id,
        )

        return {
            "dispute_id": str(dispute.dispute_id),
            "interaction_id": interaction_id,
            "filed_against": str(filed_against_uuid),
            "status": "open",
            "created_at": dispute.created_at.isoformat(),
        }


async def resolve_dispute(
    dispute_id: str,
    resolution: str,
    access_token: str,
    resolution_note: str | None = None,
) -> dict:
    """Resolve an open dispute. REQUIRES arbitrator authorization.

    The caller's access_token is verified via AgentAuth:
    1. Token introspection verifies identity
    2. trust.dispute.resolve scope is checked
    3. AgentAuth check_permission is called for 'execute' on '/trust/disputes/resolve'
       This ensures the agent is an authorized arbitrator per AgentAuth policies.

    resolution options:
    - upheld: dispute is valid; penalizes the agent filed against
    - dismissed: dispute is frivolous; slightly penalizes the filer
    - split: partial fault on both sides

    Returns updated dispute status and resolution.
    """
    provider = await _get_agentauth_provider()
    try:
        identity = await provider.authenticate(access_token=access_token)
        require_scope(identity, "trust.dispute.resolve")
    except (AuthenticationError, AuthorizationError) as e:
        return {"error": str(e)}

    is_authorized = await provider.check_permission(
        identity,
        action="execute",
        resource="/trust/disputes/resolve",
    )
    if not is_authorized:
        return {
            "error": (
                "Not authorized as arbitrator. "
                "The trust.dispute.resolve scope is required AND "
                "AgentAuth must grant 'execute' on '/trust/disputes/resolve'."
            )
        }

    if resolution not in VALID_RESOLUTIONS:
        return {"error": f"Invalid resolution. Must be one of: {sorted(VALID_RESOLUTIONS)}"}

    try:
        dispute_uuid = uuid.UUID(dispute_id)
        resolver_uuid = uuid.UUID(identity.agent_id)
    except ValueError as e:
        return {"error": f"Invalid UUID: {e}"}

    async with get_session() as session:
        result = await session.execute(
            select(Dispute).where(Dispute.dispute_id == dispute_uuid)
        )
        dispute = result.scalar_one_or_none()
        if not dispute:
            return {"error": f"Dispute not found: {dispute_id}"}
        if dispute.status != "open":
            return {"error": f"Dispute is already {dispute.status}"}

        dispute.status = "resolved"
        dispute.resolution = resolution
        dispute.resolution_note = resolution_note
        dispute.resolved_by = resolver_uuid
        dispute.resolved_at = datetime.now(UTC)

        log.info(
            "dispute_resolved",
            dispute_id=dispute_id,
            resolution=resolution,
            resolver=identity.agent_id,
        )

        try:
            await _enqueue_dispute_recomputation(
                str(dispute.filed_by),
                str(dispute.filed_against),
            )
        except Exception as e:
            log.warning("dispute_recompute_enqueue_failed", error=str(e))

        return {
            "dispute_id": dispute_id,
            "resolution": resolution,
            "resolution_note": resolution_note,
            "resolved_by": identity.agent_id,
            "resolved_at": dispute.resolved_at.isoformat(),
            "status": "resolved",
        }


async def _enqueue_dispute_recomputation(agent_id_1: str, agent_id_2: str) -> None:
    """Enqueue immediate score recomputation for both parties after dispute resolution."""
    try:
        import arq
        redis_pool = await arq.create_pool(
            arq.connections.RedisSettings.from_dsn(settings.redis_url)
        )
        await redis_pool.enqueue_job("recompute_score", agent_id_1)
        await redis_pool.enqueue_job("recompute_score", agent_id_2)
        await redis_pool.aclose()
    except Exception as e:
        log.warning("arq_enqueue_failed", error=str(e))
