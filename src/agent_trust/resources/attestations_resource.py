from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime

import structlog
from sqlalchemy import select

log = structlog.get_logger()


async def get_agent_attestations(agent_id: str) -> str:
    """Get active (non-expired, non-revoked) attestations for an agent."""
    from agent_trust.db.session import get_session
    from agent_trust.models import Agent, Attestation

    try:
        agent_uuid = uuid.UUID(agent_id)
    except ValueError:
        return json.dumps({"error": f"Invalid agent_id: {agent_id}"})

    now = datetime.now(UTC)

    async with get_session() as session:
        agent_result = await session.execute(select(Agent).where(Agent.agent_id == agent_uuid))
        if not agent_result.scalar_one_or_none():
            return json.dumps({"error": f"Agent not found: {agent_id}"})

        result = await session.execute(
            select(Attestation)
            .where(
                Attestation.subject_id == agent_uuid,
                Attestation.revoked == False,  # noqa: E712
                Attestation.valid_until > now,
            )
            .order_by(Attestation.valid_until.desc())
        )
        attestations = result.scalars().all()

        items = [
            {
                "attestation_id": str(a.attestation_id),
                "valid_from": a.valid_from.isoformat(),
                "valid_until": a.valid_until.isoformat(),
                "score_snapshot": a.score_snapshot,
                "created_at": a.created_at.isoformat(),
            }
            for a in attestations
        ]

        return json.dumps(
            {
                "agent_id": agent_id,
                "active_attestations": items,
                "count": len(items),
                "retrieved_at": now.isoformat(),
            }
        )
