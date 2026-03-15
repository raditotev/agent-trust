from __future__ import annotations

import uuid
from datetime import UTC, datetime

import structlog
from sqlalchemy import select

from agent_trust.auth.agentauth import AgentAuthProvider
from agent_trust.auth.identity import AuthenticationError, AuthorizationError
from agent_trust.auth.provider import require_scope
from agent_trust.db.redis import get_redis
from agent_trust.db.session import get_session
from agent_trust.models import Agent, AlertSubscription

log = structlog.get_logger()


async def subscribe_alerts(
    watched_agent_id: str,
    callback_tool: str,
    access_token: str,
    threshold_delta: float = 0.05,
) -> dict:
    """Subscribe to trust score change notifications for an agent.

    When the watched agent's score changes by more than threshold_delta,
    a notification is dispatched to your callback_tool.

    callback_tool: the MCP tool name on YOUR server that should be called
                   with the alert payload when a score change is detected.
                   Example: "handle_trust_alert"

    threshold_delta: minimum score change to trigger notification
                     (default 0.05 = 5 percentage points)

    Requires authentication with trust.admin scope.
    Returns subscription_id and confirmation.

    To unsubscribe, note the subscription_id (future: unsubscribe_alerts tool).
    """
    redis = await get_redis()
    provider = AgentAuthProvider(redis_client=redis)
    try:
        identity = await provider.authenticate(access_token=access_token)
        require_scope(identity, "trust.admin")
    except (AuthenticationError, AuthorizationError) as e:
        return {"error": str(e)}

    # Validate threshold
    threshold_delta = max(0.01, min(1.0, threshold_delta))

    try:
        watched_uuid = uuid.UUID(watched_agent_id)
        subscriber_uuid = uuid.UUID(identity.agent_id)
    except ValueError as e:
        return {"error": f"Invalid UUID: {e}"}

    async with get_session() as session:
        # Verify watched agent exists
        result = await session.execute(select(Agent).where(Agent.agent_id == watched_uuid))
        if not result.scalar_one_or_none():
            return {"error": f"Watched agent not found: {watched_agent_id}"}

        # Upsert subscription (unique per subscriber+watched pair)
        existing_result = await session.execute(
            select(AlertSubscription).where(
                AlertSubscription.subscriber_id == subscriber_uuid,
                AlertSubscription.watched_agent_id == watched_uuid,
            )
        )
        existing = existing_result.scalar_one_or_none()

        if existing:
            existing.callback_tool = callback_tool
            existing.threshold_delta = threshold_delta
            existing.active = True
            subscription_id = str(existing.subscription_id)
            created = False
        else:
            sub = AlertSubscription(
                subscription_id=uuid.uuid4(),
                subscriber_id=subscriber_uuid,
                watched_agent_id=watched_uuid,
                callback_tool=callback_tool,
                threshold_delta=threshold_delta,
                active=True,
                created_at=datetime.now(UTC),
            )
            session.add(sub)
            await session.flush()
            subscription_id = str(sub.subscription_id)
            created = True

        log.info(
            "alert_subscription_created" if created else "alert_subscription_updated",
            subscription_id=subscription_id,
            subscriber=identity.agent_id,
            watched=watched_agent_id,
            threshold=threshold_delta,
        )

        return {
            "subscription_id": subscription_id,
            "subscriber_id": identity.agent_id,
            "watched_agent_id": watched_agent_id,
            "callback_tool": callback_tool,
            "threshold_delta": threshold_delta,
            "active": True,
            "created": created,
        }
