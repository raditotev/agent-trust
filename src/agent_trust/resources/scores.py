from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime

import structlog
from sqlalchemy import select

log = structlog.get_logger()


async def get_agent_score(agent_id: str) -> str:
    """Get current trust scores for an agent in all categories.

    Returns JSON with scores for overall, reliability, responsiveness, honesty.
    """
    from agent_trust.db.session import get_session
    from agent_trust.models import Agent, TrustScore

    try:
        agent_uuid = uuid.UUID(agent_id)
    except ValueError:
        return json.dumps({"error": f"Invalid agent_id: {agent_id}"})

    async with get_session() as session:
        agent_result = await session.execute(select(Agent).where(Agent.agent_id == agent_uuid))
        agent = agent_result.scalar_one_or_none()
        if not agent:
            return json.dumps({"error": f"Agent not found: {agent_id}"})

        scores_result = await session.execute(
            select(TrustScore).where(TrustScore.agent_id == agent_uuid)
        )
        score_rows = scores_result.scalars().all()

        scores = {
            row.score_type: {
                "score": float(row.score),
                "confidence": float(row.confidence),
                "interaction_count": row.interaction_count,
                "computed_at": row.computed_at.isoformat(),
            }
            for row in score_rows
        }

        return json.dumps(
            {
                "agent_id": agent_id,
                "display_name": agent.display_name,
                "scores": scores,
                "retrieved_at": datetime.now(UTC).isoformat(),
            }
        )
