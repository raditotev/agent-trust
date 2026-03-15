from __future__ import annotations

import json
from datetime import UTC, datetime

import structlog
from sqlalchemy import select

log = structlog.get_logger()

VALID_SCORE_TYPES = {"overall", "reliability", "responsiveness", "honesty"}


async def get_leaderboard(score_type: str) -> str:
    """Get top 50 agents ranked by the specified score type."""
    from agent_trust.db.session import get_session
    from agent_trust.models import Agent, TrustScore

    if score_type not in VALID_SCORE_TYPES:
        return json.dumps(
            {"error": (f"Invalid score_type: {score_type}. Use one of {sorted(VALID_SCORE_TYPES)}")}
        )

    async with get_session() as session:
        result = await session.execute(
            select(Agent, TrustScore)
            .join(
                TrustScore,
                (Agent.agent_id == TrustScore.agent_id) & (TrustScore.score_type == score_type),
            )
            .where(Agent.status == "active")
            .where(TrustScore.confidence >= 0.1)
            .order_by(TrustScore.score.desc())
            .limit(50)
        )
        rows = result.all()

        entries = [
            {
                "rank": i + 1,
                "agent_id": str(agent.agent_id),
                "display_name": agent.display_name,
                "score": float(score.score),
                "confidence": float(score.confidence),
                "interaction_count": score.interaction_count,
                "auth_source": agent.auth_source,
            }
            for i, (agent, score) in enumerate(rows)
        ]

        return json.dumps(
            {
                "score_type": score_type,
                "entries": entries,
                "count": len(entries),
                "retrieved_at": datetime.now(UTC).isoformat(),
            }
        )
