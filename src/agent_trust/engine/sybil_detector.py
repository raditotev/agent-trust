from __future__ import annotations

import uuid

import structlog
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from agent_trust.models import Interaction

log = structlog.get_logger()

RING_REPORT_MIN_MUTUAL = 5
BURST_REGISTRATION_WINDOW_HOURS = 1
BURST_REGISTRATION_MIN_COUNT = 10


async def check_ring_reporting(
    agent_id: uuid.UUID,
    session: AsyncSession,
) -> bool:
    """Detect ring-reporting: A and B exclusively reporting positively for each other.

    Returns True if the agent appears to be in a reporting ring.
    """
    result = await session.execute(
        select(func.count())
        .select_from(Interaction)
        .where(
            ((Interaction.initiator_id == agent_id) | (Interaction.counterparty_id == agent_id)),
            Interaction.mutually_confirmed == True,  # noqa: E712
            Interaction.outcome == "success",
        )
    )
    mutual_success_count = result.scalar() or 0  # noqa: F841

    # Stub: full graph analysis implemented in Task 19
    return False


async def get_sybil_credibility_multiplier(
    agent_id: uuid.UUID,
    session: AsyncSession,
) -> float:
    """Return a credibility multiplier [0.0, 1.0] for Sybil-flagged agents.

    Returns 1.0 (full credibility) for clean agents.
    Returns < 1.0 for agents with detected suspicious patterns.
    """
    is_ring = await check_ring_reporting(agent_id, session)
    if is_ring:
        return 0.3
    return 1.0
