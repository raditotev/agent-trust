from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime, timedelta

import structlog
from sqlalchemy import select

log = structlog.get_logger()


async def get_agent_history(agent_id: str) -> str:
    """Get recent interaction history summary for an agent (last 90 days)."""
    from agent_trust.db.session import get_session
    from agent_trust.models import Agent, Interaction

    try:
        agent_uuid = uuid.UUID(agent_id)
    except ValueError:
        return json.dumps({"error": f"Invalid agent_id: {agent_id}"})

    cutoff = datetime.now(UTC) - timedelta(days=90)

    async with get_session() as session:
        agent_result = await session.execute(select(Agent).where(Agent.agent_id == agent_uuid))
        if not agent_result.scalar_one_or_none():
            return json.dumps({"error": f"Agent not found: {agent_id}"})

        result = await session.execute(
            select(Interaction)
            .where(
                (Interaction.initiator_id == agent_uuid)
                | (Interaction.counterparty_id == agent_uuid),
                Interaction.reported_at >= cutoff,
            )
            .order_by(Interaction.reported_at.desc())
            .limit(100)
        )
        interactions = result.scalars().all()

        outcomes: dict[str, int] = {}
        types: dict[str, int] = {}
        mutual_count = 0
        for ix in interactions:
            outcomes[ix.outcome] = outcomes.get(ix.outcome, 0) + 1
            types[ix.interaction_type] = types.get(ix.interaction_type, 0) + 1
            if ix.mutually_confirmed:
                mutual_count += 1

        items = [
            {
                "interaction_id": str(ix.interaction_id),
                "role": "initiator" if ix.initiator_id == agent_uuid else "counterparty",
                "counterparty_id": str(
                    ix.counterparty_id if ix.initiator_id == agent_uuid else ix.initiator_id
                ),
                "interaction_type": ix.interaction_type,
                "outcome": ix.outcome,
                "mutually_confirmed": ix.mutually_confirmed,
                "reported_at": ix.reported_at.isoformat(),
            }
            for ix in interactions
        ]

        return json.dumps(
            {
                "agent_id": agent_id,
                "period_days": 90,
                "total_interactions": len(items),
                "mutually_confirmed": mutual_count,
                "outcome_breakdown": outcomes,
                "type_breakdown": types,
                "interactions": items,
                "retrieved_at": datetime.now(UTC).isoformat(),
            }
        )
