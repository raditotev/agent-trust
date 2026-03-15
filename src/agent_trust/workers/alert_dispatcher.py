from __future__ import annotations

import uuid
from datetime import UTC, datetime

import structlog
from sqlalchemy import select

log = structlog.get_logger()


async def dispatch_alerts(ctx: dict, agent_id: str, old_score: float, new_score: float) -> dict:
    """arq worker task: Dispatch alerts when an agent's score changes significantly.

    Compares old vs new score against each subscriber's threshold_delta.
    If the change exceeds the threshold, logs the alert (in production,
    would call the subscriber's callback_tool via MCP).
    """
    from agent_trust.db.session import get_session
    from agent_trust.models import AlertSubscription

    delta = abs(new_score - old_score)
    log.info("alert_check", agent_id=agent_id, delta=round(delta, 4))

    if delta == 0:
        return {"agent_id": agent_id, "delta": 0.0, "alerts_dispatched": 0}

    try:
        watched_uuid = uuid.UUID(agent_id)
    except ValueError:
        return {"error": f"Invalid agent_id: {agent_id}"}

    alerts_dispatched = 0
    subscriptions = []
    async with get_session() as session:
        result = await session.execute(
            select(AlertSubscription).where(
                AlertSubscription.watched_agent_id == watched_uuid,
                AlertSubscription.active == True,  # noqa: E712
            )
        )
        subscriptions = result.scalars().all()

        for sub in subscriptions:
            if delta >= float(sub.threshold_delta):
                alert_payload = {
                    "watched_agent_id": agent_id,
                    "old_score": round(old_score, 4),
                    "new_score": round(new_score, 4),
                    "delta": round(delta, 4),
                    "callback_tool": sub.callback_tool,
                    "subscriber_id": str(sub.subscriber_id),
                    "timestamp": datetime.now(UTC).isoformat(),
                }
                log.info(
                    "alert_dispatched",
                    **{k: v for k, v in alert_payload.items() if k != "timestamp"},
                )
                alerts_dispatched += 1

    return {
        "agent_id": agent_id,
        "delta": round(delta, 4),
        "alerts_dispatched": alerts_dispatched,
        "subscriptions_checked": len(subscriptions),
    }
