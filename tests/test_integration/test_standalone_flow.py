from __future__ import annotations

import uuid
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from agent_trust.auth.identity import AgentIdentity, AuthenticationError
from agent_trust.ratelimit import RateLimitResult
from tests.factories import make_agent, make_trust_score

RATE_LIMIT_ALLOWED = RateLimitResult(allowed=True, limit=60, remaining=59, reset_at=9_999_999_999)


@pytest.mark.asyncio
async def test_register_and_whoami_agentauth_flow():
    """Full flow: register via AgentAuth token, then call whoami."""
    from agent_trust.tools.agents import register_agent, whoami

    agent_id = str(uuid.uuid4())
    mock_identity = AgentIdentity(
        agent_id=agent_id,
        source="agentauth",
        scopes=["trust.read", "trust.report"],
        trust_level="delegated",
    )

    mock_agent = make_agent(agent_id=uuid.UUID(agent_id), auth_source="agentauth")

    with (
        patch("agent_trust.tools.agents._resolve_identity", return_value=mock_identity),
        patch("agent_trust.tools.agents._ensure_agent_profile", return_value=(mock_agent, True)),
        patch("agent_trust.tools.agents.get_session"),
        patch(
            "agent_trust.tools.agents.check_rate_limit",
            new=AsyncMock(return_value=RATE_LIMIT_ALLOWED),
        ),
    ):
        reg_result = await register_agent(
            display_name="Test Agent",
            access_token="mock-token",
        )
        assert reg_result["agent_id"] == agent_id
        assert reg_result["source"] == "agentauth"
        assert reg_result["created"] is True

    # Now test whoami
    mock_score = make_trust_score(uuid.UUID(agent_id))

    async def mock_session_execute(stmt):
        result = MagicMock()
        result.scalar_one_or_none.return_value = mock_agent
        result.scalars.return_value.all.return_value = [mock_score]
        return result

    mock_session = AsyncMock()
    mock_session.execute = mock_session_execute
    mock_context = AsyncMock()
    mock_context.__aenter__ = AsyncMock(return_value=mock_session)
    mock_context.__aexit__ = AsyncMock(return_value=False)

    with (
        patch("agent_trust.tools.agents._resolve_identity", return_value=mock_identity),
        patch("agent_trust.tools.agents.get_session", return_value=mock_context),
    ):
        whoami_result = await whoami(access_token="mock-token")
        assert whoami_result["agent_id"] == agent_id
        assert whoami_result["source"] == "agentauth"


@pytest.mark.asyncio
async def test_register_standalone_flow():
    """Standalone flow: register with Ed25519 key."""
    from agent_trust.tools.agents import register_agent

    agent_id = uuid.uuid4()
    mock_identity = AgentIdentity(
        agent_id=str(agent_id),
        source="standalone",
        scopes=["trust.read", "trust.report"],
        trust_level="standalone",
    )
    mock_agent = make_agent(agent_id=agent_id, auth_source="standalone")

    with (
        patch("agent_trust.tools.agents._resolve_identity", return_value=mock_identity),
        patch("agent_trust.tools.agents._ensure_agent_profile", return_value=(mock_agent, True)),
        patch(
            "agent_trust.tools.agents.check_rate_limit",
            new=AsyncMock(return_value=RATE_LIMIT_ALLOWED),
        ),
    ):
        result = await register_agent(public_key_hex="deadbeef01020304")
        assert result["source"] == "standalone"
        assert "trust.read" in result["scopes"]


@pytest.mark.asyncio
async def test_unauthenticated_raises():
    """Calling register_agent with no credentials raises AuthenticationError."""
    from agent_trust.tools.agents import register_agent

    with (
        patch(
            "agent_trust.tools.agents.check_rate_limit",
            new=AsyncMock(return_value=RATE_LIMIT_ALLOWED),
        ),
        patch("agent_trust.tools.agents.settings") as mock_settings,
    ):
        mock_settings.auth_provider = "agentauth"
        with pytest.raises(AuthenticationError):
            await register_agent()
