from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime

import structlog
from sqlalchemy import select

from agent_trust.tools.alerts import PERMITTED_CALLBACK_TOOLS

log = structlog.get_logger()


async def _deliver_alert_via_redis(subscriber_id: str, alert_payload: dict) -> bool:
    """Deliver alert by publishing to a Redis channel for the subscriber.

    Subscribers can listen on channel 'alerts:{subscriber_id}' to receive
    real-time push notifications. Returns True on success.
    """
    try:
        from agent_trust.db.redis import get_redis

        redis = await get_redis()
        channel = f"alerts:{subscriber_id}"
        await redis.publish(channel, json.dumps(alert_payload))
        log.debug("alert_published_redis", channel=channel, subscriber_id=subscriber_id)
        return True
    except Exception as e:
        log.warning("alert_redis_publish_failed", subscriber_id=subscriber_id, error=str(e))
        return False


async def dispatch_alerts(ctx: dict, agent_id: str, old_score: float, new_score: float) -> dict:
    """arq worker task: Dispatch alerts when an agent's score changes significantly.

    Compares old vs new score against each subscriber's threshold_delta.
    If the change exceeds the threshold, delivers the alert via Redis pub/sub
    and logs it for audit. Subscribers listen on 'alerts:{subscriber_id}'.
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
    alerts_failed = 0
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
            # Defense in depth: re-validate callback_tool against allowlist before dispatching
            if sub.callback_tool not in PERMITTED_CALLBACK_TOOLS:
                log.warning(
                    "alert_dispatch_blocked_invalid_callback",
                    subscriber_id=str(sub.subscriber_id),
                    callback_tool=sub.callback_tool,
                )
                continue

            if delta >= float(sub.threshold_delta):
                alert_payload = {
                    "type": "trust_score_change",
                    "watched_agent_id": agent_id,
                    "old_score": round(old_score, 4),
                    "new_score": round(new_score, 4),
                    "delta": round(delta, 4),
                    "direction": "up" if new_score > old_score else "down",
                    "callback_tool": sub.callback_tool,
                    "subscriber_id": str(sub.subscriber_id),
                    "timestamp": datetime.now(UTC).isoformat(),
                }

                delivered = await _deliver_alert_via_redis(str(sub.subscriber_id), alert_payload)

                if delivered:
                    alerts_dispatched += 1
                else:
                    alerts_failed += 1

                log.info(
                    "alert_dispatched",
                    delivered=delivered,
                    **{k: v for k, v in alert_payload.items() if k != "timestamp"},
                )

    return {
        "agent_id": agent_id,
        "delta": round(delta, 4),
        "alerts_dispatched": alerts_dispatched,
        "alerts_failed": alerts_failed,
        "subscriptions_checked": len(subscriptions),
    }
