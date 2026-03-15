from __future__ import annotations

from datetime import UTC, datetime

import structlog
from sqlalchemy import select

from agent_trust.db.session import get_session
from agent_trust.models import Attestation

log = structlog.get_logger()


async def expire_attestations(ctx: dict) -> dict:
    """arq periodic task: Mark expired attestations as revoked.

    Scans for attestations past their valid_until time and marks them revoked.
    Full implementation in Task 16.
    """
    now = datetime.now(UTC)
    revoked_count = 0

    async with get_session() as session:
        result = await session.execute(
            select(Attestation).where(
                Attestation.valid_until < now,
                Attestation.revoked == False,  # noqa: E712
            )
        )
        expired = result.scalars().all()
        for attestation in expired:
            attestation.revoked = True
            revoked_count += 1

    log.info("attestations_expired", count=revoked_count)
    return {"revoked": revoked_count}
