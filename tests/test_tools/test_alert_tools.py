from __future__ import annotations

import uuid
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from agent_trust.auth.identity import AgentIdentity


@pytest.mark.asyncio
async def test_subscribe_alerts_success():
    from agent_trust.tools.alerts import subscribe_alerts

    agent_id = str(uuid.uuid4())
    watched_id = str(uuid.uuid4())
    identity = AgentIdentity(
        agent_id=agent_id,
        source="agentauth",
        scopes=["trust.admin"],
        trust_level="root",
    )

    mock_agent = MagicMock()
    mock_execute_result = MagicMock()
    mock_execute_result.scalar_one_or_none.side_effect = [mock_agent, None]

    mock_session = AsyncMock()
    mock_session.execute = AsyncMock(return_value=mock_execute_result)
    mock_session.flush = AsyncMock()

    mock_ctx = AsyncMock()
    mock_ctx.__aenter__ = AsyncMock(return_value=mock_session)
    mock_ctx.__aexit__ = AsyncMock(return_value=False)

    with (
        patch("agent_trust.tools.alerts.AgentAuthProvider") as mock_provider_cls,
        patch("agent_trust.tools.alerts.get_redis", return_value=AsyncMock()),
        patch("agent_trust.tools.alerts.get_session", return_value=mock_ctx),
    ):
        mock_provider = AsyncMock()
        mock_provider.authenticate = AsyncMock(return_value=identity)
        mock_provider_cls.return_value = mock_provider

        result = await subscribe_alerts(
            watched_agent_id=watched_id,
            callback_tool="notify_agent",
            access_token="mock-token",
            threshold_delta=0.1,
        )

    assert "error" not in result
    assert result["callback_tool"] == "notify_agent"
    assert result["threshold_delta"] == 0.1
    assert result["watched_agent_id"] == watched_id
    assert result["created"] is True


@pytest.mark.asyncio
async def test_subscribe_alerts_upsert_existing():
    from agent_trust.tools.alerts import subscribe_alerts

    agent_id = str(uuid.uuid4())
    watched_id = str(uuid.uuid4())
    identity = AgentIdentity(
        agent_id=agent_id,
        source="agentauth",
        scopes=["trust.admin"],
        trust_level="root",
    )

    mock_agent = MagicMock()
    existing_sub = MagicMock()
    existing_sub.subscription_id = uuid.uuid4()
    existing_sub.callback_tool = "old_tool"
    existing_sub.threshold_delta = 0.05
    existing_sub.active = True

    mock_execute_result = MagicMock()
    mock_execute_result.scalar_one_or_none.side_effect = [mock_agent, existing_sub]

    mock_session = AsyncMock()
    mock_session.execute = AsyncMock(return_value=mock_execute_result)

    mock_ctx = AsyncMock()
    mock_ctx.__aenter__ = AsyncMock(return_value=mock_session)
    mock_ctx.__aexit__ = AsyncMock(return_value=False)

    with (
        patch("agent_trust.tools.alerts.AgentAuthProvider") as mock_provider_cls,
        patch("agent_trust.tools.alerts.get_redis", return_value=AsyncMock()),
        patch("agent_trust.tools.alerts.get_session", return_value=mock_ctx),
    ):
        mock_provider = AsyncMock()
        mock_provider.authenticate = AsyncMock(return_value=identity)
        mock_provider_cls.return_value = mock_provider

        result = await subscribe_alerts(
            watched_agent_id=watched_id,
            callback_tool="notify_agent",
            access_token="mock-token",
            threshold_delta=0.2,
        )

    assert "error" not in result
    assert result["created"] is False
    assert result["callback_tool"] == "notify_agent"
    assert existing_sub.callback_tool == "notify_agent"
    assert existing_sub.threshold_delta == 0.2


@pytest.mark.asyncio
async def test_subscribe_alerts_requires_admin_scope():
    from agent_trust.tools.alerts import subscribe_alerts

    identity = AgentIdentity(
        agent_id=str(uuid.uuid4()),
        source="agentauth",
        scopes=["trust.read"],  # missing trust.admin
        trust_level="delegated",
    )

    with (
        patch("agent_trust.tools.alerts.AgentAuthProvider") as mock_provider_cls,
        patch("agent_trust.tools.alerts.get_redis", return_value=AsyncMock()),
    ):
        mock_provider = AsyncMock()
        mock_provider.authenticate = AsyncMock(return_value=identity)
        mock_provider_cls.return_value = mock_provider

        result = await subscribe_alerts(
            watched_agent_id=str(uuid.uuid4()),
            callback_tool="notify_agent",
            access_token="mock-token",
        )

    assert "error" in result


@pytest.mark.asyncio
async def test_subscribe_alerts_watched_not_found():
    from agent_trust.tools.alerts import subscribe_alerts

    identity = AgentIdentity(
        agent_id=str(uuid.uuid4()),
        source="agentauth",
        scopes=["trust.admin"],
        trust_level="root",
    )

    mock_result = MagicMock()
    mock_result.scalar_one_or_none.return_value = None  # agent not found

    mock_session = AsyncMock()
    mock_session.execute = AsyncMock(return_value=mock_result)

    mock_ctx = AsyncMock()
    mock_ctx.__aenter__ = AsyncMock(return_value=mock_session)
    mock_ctx.__aexit__ = AsyncMock(return_value=False)

    with (
        patch("agent_trust.tools.alerts.AgentAuthProvider") as mock_provider_cls,
        patch("agent_trust.tools.alerts.get_redis", return_value=AsyncMock()),
        patch("agent_trust.tools.alerts.get_session", return_value=mock_ctx),
    ):
        mock_provider = AsyncMock()
        mock_provider.authenticate = AsyncMock(return_value=identity)
        mock_provider_cls.return_value = mock_provider

        result = await subscribe_alerts(
            watched_agent_id=str(uuid.uuid4()),
            callback_tool="alert_handler",
            access_token="mock-token",
        )

    assert "error" in result
    assert "not found" in result["error"]


@pytest.mark.asyncio
async def test_subscribe_alerts_threshold_clamping():
    from agent_trust.tools.alerts import subscribe_alerts

    agent_id = str(uuid.uuid4())
    watched_id = str(uuid.uuid4())
    identity = AgentIdentity(
        agent_id=agent_id,
        source="agentauth",
        scopes=["trust.admin"],
        trust_level="root",
    )

    mock_agent = MagicMock()
    mock_execute_result = MagicMock()
    mock_execute_result.scalar_one_or_none.side_effect = [mock_agent, None]

    mock_session = AsyncMock()
    mock_session.execute = AsyncMock(return_value=mock_execute_result)
    mock_session.flush = AsyncMock()

    mock_ctx = AsyncMock()
    mock_ctx.__aenter__ = AsyncMock(return_value=mock_session)
    mock_ctx.__aexit__ = AsyncMock(return_value=False)

    with (
        patch("agent_trust.tools.alerts.AgentAuthProvider") as mock_provider_cls,
        patch("agent_trust.tools.alerts.get_redis", return_value=AsyncMock()),
        patch("agent_trust.tools.alerts.get_session", return_value=mock_ctx),
    ):
        mock_provider = AsyncMock()
        mock_provider.authenticate = AsyncMock(return_value=identity)
        mock_provider_cls.return_value = mock_provider

        # threshold_delta=0.0 should be clamped to 0.01
        result = await subscribe_alerts(
            watched_agent_id=watched_id,
            callback_tool="notify_agent",
            access_token="mock-token",
            threshold_delta=0.0,
        )

    assert "error" not in result
    assert result["threshold_delta"] == 0.01


@pytest.mark.asyncio
async def test_dispatch_alerts_no_subscriptions():
    from agent_trust.workers.alert_dispatcher import dispatch_alerts

    agent_id = str(uuid.uuid4())

    mock_scalars = MagicMock()
    mock_scalars.all.return_value = []

    mock_result = MagicMock()
    mock_result.scalars.return_value = mock_scalars

    mock_session = AsyncMock()
    mock_session.execute = AsyncMock(return_value=mock_result)

    mock_ctx = AsyncMock()
    mock_ctx.__aenter__ = AsyncMock(return_value=mock_session)
    mock_ctx.__aexit__ = AsyncMock(return_value=False)

    with patch("agent_trust.db.session.get_session", return_value=mock_ctx):
        result = await dispatch_alerts({}, agent_id, 0.70, 0.80)

    assert result["alerts_dispatched"] == 0
    assert result["subscriptions_checked"] == 0
    assert result["delta"] == 0.1


@pytest.mark.asyncio
async def test_dispatch_alerts_zero_delta():
    from agent_trust.workers.alert_dispatcher import dispatch_alerts

    agent_id = str(uuid.uuid4())
    result = await dispatch_alerts({}, agent_id, 0.75, 0.75)

    assert result["delta"] == 0.0
    assert result["alerts_dispatched"] == 0


@pytest.mark.asyncio
async def test_dispatch_alerts_dispatches_when_threshold_exceeded():
    from agent_trust.workers.alert_dispatcher import dispatch_alerts

    agent_id = str(uuid.uuid4())

    sub = MagicMock()
    sub.threshold_delta = 0.05
    sub.callback_tool = "notify_agent"
    sub.subscriber_id = uuid.uuid4()

    mock_scalars = MagicMock()
    mock_scalars.all.return_value = [sub]

    mock_result = MagicMock()
    mock_result.scalars.return_value = mock_scalars

    mock_session = AsyncMock()
    mock_session.execute = AsyncMock(return_value=mock_result)

    mock_ctx = AsyncMock()
    mock_ctx.__aenter__ = AsyncMock(return_value=mock_session)
    mock_ctx.__aexit__ = AsyncMock(return_value=False)

    with (
        patch("agent_trust.db.session.get_session", return_value=mock_ctx),
        patch(
            "agent_trust.workers.alert_dispatcher._deliver_alert_via_redis",
            new=AsyncMock(return_value=True),
        ),
    ):
        result = await dispatch_alerts({}, agent_id, 0.60, 0.80)

    assert result["alerts_dispatched"] == 1
    assert result["subscriptions_checked"] == 1


@pytest.mark.asyncio
async def test_dispatch_alerts_skips_below_threshold():
    from agent_trust.workers.alert_dispatcher import dispatch_alerts

    agent_id = str(uuid.uuid4())

    sub = MagicMock()
    sub.threshold_delta = 0.20  # high threshold
    sub.callback_tool = "notify_agent"
    sub.subscriber_id = uuid.uuid4()

    mock_scalars = MagicMock()
    mock_scalars.all.return_value = [sub]

    mock_result = MagicMock()
    mock_result.scalars.return_value = mock_scalars

    mock_session = AsyncMock()
    mock_session.execute = AsyncMock(return_value=mock_result)

    mock_ctx = AsyncMock()
    mock_ctx.__aenter__ = AsyncMock(return_value=mock_session)
    mock_ctx.__aexit__ = AsyncMock(return_value=False)

    with patch("agent_trust.db.session.get_session", return_value=mock_ctx):
        # delta = 0.05, threshold = 0.20 → no dispatch
        result = await dispatch_alerts({}, agent_id, 0.70, 0.75)

    assert result["alerts_dispatched"] == 0
    assert result["subscriptions_checked"] == 1
