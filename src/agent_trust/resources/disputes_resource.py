from __future__ import annotations

import json
import uuid

import structlog
from sqlalchemy import select

log = structlog.get_logger()


async def get_dispute(dispute_id: str) -> str:
    """Get full details of a specific dispute."""
    from agent_trust.db.session import get_session
    from agent_trust.models import Dispute

    try:
        dispute_uuid = uuid.UUID(dispute_id)
    except ValueError:
        return json.dumps({"error": f"Invalid dispute_id: {dispute_id}"})

    async with get_session() as session:
        result = await session.execute(select(Dispute).where(Dispute.dispute_id == dispute_uuid))
        dispute = result.scalar_one_or_none()
        if not dispute:
            return json.dumps({"error": f"Dispute not found: {dispute_id}"})

        return json.dumps(
            {
                "dispute_id": str(dispute.dispute_id),
                "interaction_id": str(dispute.interaction_id),
                "filed_by": str(dispute.filed_by),
                "filed_against": str(dispute.filed_against),
                "reason": dispute.reason,
                "evidence": dispute.evidence,
                "status": dispute.status,
                "resolution": dispute.resolution,
                "resolution_note": dispute.resolution_note,
                "resolved_by": str(dispute.resolved_by) if dispute.resolved_by else None,
                "resolved_at": dispute.resolved_at.isoformat() if dispute.resolved_at else None,
                "created_at": dispute.created_at.isoformat(),
            }
        )
