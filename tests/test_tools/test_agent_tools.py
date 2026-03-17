from __future__ import annotations

import uuid
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from agent_trust.auth.identity import AgentIdentity, AuthenticationError
from agent_trust.auth.standalone import STANDALONE_SCOPES
from agent_trust.tools.agents import (
    get_agent_profile,
    link_agentauth,
    register_agent,
    search_agents,
    whoami,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_AGENT_SCOPES = ["trust.read", "trust.report", "trust.dispute.file"]
_AA_AGENT_ID = str(uuid.uuid4())
_STANDALONE_AGENT_ID = str(uuid.uuid4())


def _make_identity(agent_id: str = _AA_AGENT_ID, source: str = "agentauth") -> AgentIdentity:
    return AgentIdentity(
        agent_id=agent_id,
        source=source,
        scopes=_AGENT_SCOPES if source == "agentauth" else STANDALONE_SCOPES,
        trust_level="delegated" if source == "agentauth" else "standalone",
    )


def _make_agent(agent_id: str, source: str = "agentauth") -> MagicMock:
    agent = MagicMock()
    agent.agent_id = uuid.UUID(agent_id)
    agent.display_name = "Test Agent"
    agent.auth_source = source
    agent.agentauth_linked = source == "agentauth"
    agent.capabilities = ["coding"]
    agent.metadata_ = {}
    agent.trust_level = 0.75
    agent.status = "active"
    agent.registered_at = None
    agent.public_key = bytes.fromhex("ab" * 32)
    return agent


def _make_trust_score(agent_id: str, score_type: str = "overall") -> MagicMock:
    ts = MagicMock()
    ts.agent_id = uuid.UUID(agent_id)
    ts.score_type = score_type
    ts.score = 0.8
    ts.confidence = 0.9
    ts.interaction_count = 42
    return ts


# ---------------------------------------------------------------------------
# register_agent
# ---------------------------------------------------------------------------


class TestRegisterAgent:
    @pytest.mark.asyncio
    async def test_register_agent_agentauth_path(self):
        """AgentAuth token path creates a profile from introspection."""
        identity = _make_identity()
        agent = _make_agent(_AA_AGENT_ID)

        with (
            patch(
                "agent_trust.tools.agents._resolve_identity",
                new=AsyncMock(return_value=identity),
            ),
            patch(
                "agent_trust.tools.agents._ensure_agent_profile",
                new=AsyncMock(return_value=(agent, True)),
            ),
        ):
            result = await register_agent(access_token="tok")

        assert result["agent_id"] == _AA_AGENT_ID
        assert result["source"] == "agentauth"
        assert result["created"] is True
        assert "trust.read" in result["scopes"]

    @pytest.mark.asyncio
    async def test_register_agent_standalone_path(self):
        """Standalone path creates a profile from a hex public key."""
        agent_id = str(uuid.uuid4())
        agent = _make_agent(agent_id, source="standalone")

        with patch(
            "agent_trust.tools.agents._ensure_agent_profile",
            new=AsyncMock(return_value=(agent, True)),
        ):
            result = await register_agent(public_key_hex="ab" * 32)

        assert result["source"] == "standalone"
        assert result["created"] is True
        assert result["scopes"] == STANDALONE_SCOPES

    @pytest.mark.asyncio
    async def test_register_agent_idempotent(self):
        """Registering twice returns the same agent_id and created=False."""
        identity = _make_identity()
        agent = _make_agent(_AA_AGENT_ID)

        with (
            patch(
                "agent_trust.tools.agents._resolve_identity",
                new=AsyncMock(return_value=identity),
            ),
            patch(
                "agent_trust.tools.agents._ensure_agent_profile",
                new=AsyncMock(return_value=(agent, False)),
            ),
        ):
            result = await register_agent(access_token="tok")

        assert result["agent_id"] == _AA_AGENT_ID
        assert result["created"] is False

    @pytest.mark.asyncio
    async def test_register_agent_no_auth_autogenerates_keypair(self):
        """No token, no key → auto-generates Ed25519 key pair (standalone mode)."""
        agent_id = str(uuid.uuid4())
        agent = _make_agent(agent_id, source="standalone")

        with (
            patch("agent_trust.tools.agents.settings") as mock_settings,
            patch(
                "agent_trust.tools.agents._ensure_agent_profile",
                new=AsyncMock(return_value=(agent, True)),
            ),
        ):
            mock_settings.auth_provider = "both"
            result = await register_agent(display_name="auto-agent")

        assert result["source"] == "standalone"
        assert result["created"] is True
        assert "public_key_hex" in result
        assert "private_key_hex" in result
        assert "warning" in result
        assert len(bytes.fromhex(result["public_key_hex"])) == 32
        assert len(bytes.fromhex(result["private_key_hex"])) == 32

    @pytest.mark.asyncio
    async def test_register_agent_no_auth_raises_when_agentauth_only(self):
        """No token, no key → AuthenticationError when auth_provider=agentauth."""
        with patch("agent_trust.tools.agents.settings") as mock_settings:
            mock_settings.auth_provider = "agentauth"
            with pytest.raises(AuthenticationError):
                await register_agent()

    @pytest.mark.asyncio
    async def test_register_agent_invalid_hex_raises(self):
        """Invalid hex key → AuthenticationError."""
        with pytest.raises(AuthenticationError, match="Invalid public_key_hex"):
            await register_agent(public_key_hex="not-valid-hex!!")


# ---------------------------------------------------------------------------
# link_agentauth
# ---------------------------------------------------------------------------


class TestLinkAgentauth:
    @pytest.mark.asyncio
    async def test_link_agentauth_success(self):
        """Standalone profile is updated with AgentAuth identity."""
        aa_identity = _make_identity(_AA_AGENT_ID, source="agentauth")
        standalone_agent = _make_agent(_STANDALONE_AGENT_ID, source="standalone")
        standalone_agent.public_key = bytes.fromhex("cd" * 32)
        standalone_agent.agent_id = uuid.UUID(_STANDALONE_AGENT_ID)

        mock_session = AsyncMock()
        mock_result = MagicMock()
        mock_result.scalar_one_or_none.return_value = standalone_agent
        mock_session.execute = AsyncMock(return_value=mock_result)
        mock_ctx = AsyncMock()
        mock_ctx.__aenter__ = AsyncMock(return_value=mock_session)
        mock_ctx.__aexit__ = AsyncMock(return_value=False)

        with (
            patch(
                "agent_trust.tools.agents.AgentAuthProvider.authenticate",
                new=AsyncMock(return_value=aa_identity),
            ),
            patch("agent_trust.tools.agents.get_redis", new=AsyncMock(return_value=AsyncMock())),
            patch("agent_trust.tools.agents.get_session", return_value=mock_ctx),
        ):
            result = await link_agentauth(access_token="tok", public_key_hex="cd" * 32)

        assert result["merged"] is True
        assert result["agentauth_id"] == _AA_AGENT_ID

    @pytest.mark.asyncio
    async def test_link_agentauth_unknown_key_raises(self):
        """Unknown public key raises AuthenticationError."""
        aa_identity = _make_identity(_AA_AGENT_ID)

        mock_session = AsyncMock()
        mock_result = MagicMock()
        mock_result.scalar_one_or_none.return_value = None
        mock_session.execute = AsyncMock(return_value=mock_result)
        mock_ctx = AsyncMock()
        mock_ctx.__aenter__ = AsyncMock(return_value=mock_session)
        mock_ctx.__aexit__ = AsyncMock(return_value=False)

        with (
            patch(
                "agent_trust.tools.agents.AgentAuthProvider.authenticate",
                new=AsyncMock(return_value=aa_identity),
            ),
            patch("agent_trust.tools.agents.get_redis", new=AsyncMock(return_value=AsyncMock())),
            patch("agent_trust.tools.agents.get_session", return_value=mock_ctx),
        ):
            with pytest.raises(AuthenticationError, match="No standalone agent"):
                await link_agentauth(access_token="tok", public_key_hex="ab" * 32)


# ---------------------------------------------------------------------------
# whoami
# ---------------------------------------------------------------------------


class TestWhoami:
    @pytest.mark.asyncio
    async def test_whoami_returns_identity(self):
        """whoami returns all expected fields for a registered agent."""
        identity = _make_identity()
        agent = _make_agent(_AA_AGENT_ID)
        ts = _make_trust_score(_AA_AGENT_ID)

        mock_session = AsyncMock()
        # First execute → agent, second execute → scores
        agent_result = MagicMock()
        agent_result.scalar_one_or_none.return_value = agent
        scores_result = MagicMock()
        scores_result.scalars.return_value.all.return_value = [ts]
        mock_session.execute = AsyncMock(side_effect=[agent_result, scores_result])
        mock_ctx = AsyncMock()
        mock_ctx.__aenter__ = AsyncMock(return_value=mock_session)
        mock_ctx.__aexit__ = AsyncMock(return_value=False)

        with (
            patch(
                "agent_trust.tools.agents._resolve_identity",
                new=AsyncMock(return_value=identity),
            ),
            patch("agent_trust.tools.agents.get_session", return_value=mock_ctx),
        ):
            result = await whoami(access_token="tok")

        assert result["agent_id"] == _AA_AGENT_ID
        assert result["source"] == "agentauth"
        assert result["scopes"] == _AGENT_SCOPES
        assert "scores" in result
        assert result["interaction_count"] == 42

    @pytest.mark.asyncio
    async def test_whoami_no_profile_returns_note(self):
        """whoami for an unregistered agent returns a note field."""
        identity = _make_identity()

        mock_session = AsyncMock()
        agent_result = MagicMock()
        agent_result.scalar_one_or_none.return_value = None
        mock_session.execute = AsyncMock(return_value=agent_result)
        mock_ctx = AsyncMock()
        mock_ctx.__aenter__ = AsyncMock(return_value=mock_session)
        mock_ctx.__aexit__ = AsyncMock(return_value=False)

        with (
            patch(
                "agent_trust.tools.agents._resolve_identity",
                new=AsyncMock(return_value=identity),
            ),
            patch("agent_trust.tools.agents.get_session", return_value=mock_ctx),
        ):
            result = await whoami(access_token="tok")

        assert "note" in result
        assert result["scores"] == {}


# ---------------------------------------------------------------------------
# get_agent_profile
# ---------------------------------------------------------------------------


class TestGetAgentProfile:
    @pytest.mark.asyncio
    async def test_get_agent_profile_found(self):
        """Returns full profile for an existing agent."""
        agent = _make_agent(_AA_AGENT_ID)
        ts = _make_trust_score(_AA_AGENT_ID)

        mock_session = AsyncMock()
        agent_result = MagicMock()
        agent_result.scalar_one_or_none.return_value = agent
        scores_result = MagicMock()
        scores_result.scalars.return_value.all.return_value = [ts]
        mock_session.execute = AsyncMock(side_effect=[agent_result, scores_result])
        mock_ctx = AsyncMock()
        mock_ctx.__aenter__ = AsyncMock(return_value=mock_session)
        mock_ctx.__aexit__ = AsyncMock(return_value=False)

        with patch("agent_trust.tools.agents.get_session", return_value=mock_ctx):
            result = await get_agent_profile(_AA_AGENT_ID)

        assert result["agent_id"] == _AA_AGENT_ID
        assert result["status"] == "active"
        assert "scores" in result

    @pytest.mark.asyncio
    async def test_get_agent_profile_not_found(self):
        """Returns error dict when agent does not exist."""
        mock_session = AsyncMock()
        agent_result = MagicMock()
        agent_result.scalar_one_or_none.return_value = None
        scores_result = MagicMock()
        scores_result.scalars.return_value.all.return_value = []
        mock_session.execute = AsyncMock(side_effect=[agent_result, scores_result])
        mock_ctx = AsyncMock()
        mock_ctx.__aenter__ = AsyncMock(return_value=mock_session)
        mock_ctx.__aexit__ = AsyncMock(return_value=False)

        with patch("agent_trust.tools.agents.get_session", return_value=mock_ctx):
            result = await get_agent_profile(_AA_AGENT_ID)

        assert result["error"] == "not_found"

    @pytest.mark.asyncio
    async def test_get_agent_profile_invalid_uuid(self):
        """Invalid UUID returns error dict without DB call."""
        result = await get_agent_profile("not-a-uuid")
        assert result["error"] == "invalid_agent_id"

    @pytest.mark.asyncio
    async def test_get_agent_profile_full_detail_when_authenticated(self):
        """Authenticated calls include agentauth_linked and auth_source."""
        identity = _make_identity()
        agent = _make_agent(_AA_AGENT_ID)

        mock_session = AsyncMock()
        agent_result = MagicMock()
        agent_result.scalar_one_or_none.return_value = agent
        scores_result = MagicMock()
        scores_result.scalars.return_value.all.return_value = []
        mock_session.execute = AsyncMock(side_effect=[agent_result, scores_result])
        mock_ctx = AsyncMock()
        mock_ctx.__aenter__ = AsyncMock(return_value=mock_session)
        mock_ctx.__aexit__ = AsyncMock(return_value=False)

        with (
            patch(
                "agent_trust.tools.agents._resolve_identity",
                new=AsyncMock(return_value=identity),
            ),
            patch("agent_trust.tools.agents.get_session", return_value=mock_ctx),
        ):
            result = await get_agent_profile(_AA_AGENT_ID, access_token="tok")

        assert "agentauth_linked" in result
        assert "auth_source" in result


# ---------------------------------------------------------------------------
# search_agents
# ---------------------------------------------------------------------------


class TestSearchAgents:
    @pytest.mark.asyncio
    async def test_search_agents_returns_results(self):
        """Returns list of agents matching criteria."""
        agent = _make_agent(_AA_AGENT_ID)
        ts = _make_trust_score(_AA_AGENT_ID)

        mock_session = AsyncMock()
        rows_result = MagicMock()
        rows_result.all.return_value = [(agent, ts)]
        mock_session.execute = AsyncMock(return_value=rows_result)
        mock_ctx = AsyncMock()
        mock_ctx.__aenter__ = AsyncMock(return_value=mock_session)
        mock_ctx.__aexit__ = AsyncMock(return_value=False)

        with patch("agent_trust.tools.agents.get_session", return_value=mock_ctx):
            result = await search_agents(min_score=0.5, limit=10)

        assert result["total"] == 1
        assert result["agents"][0]["agent_id"] == _AA_AGENT_ID
        assert result["filters"]["min_score"] == 0.5

    @pytest.mark.asyncio
    async def test_search_agents_empty_results(self):
        """Returns empty list when no agents match."""
        mock_session = AsyncMock()
        rows_result = MagicMock()
        rows_result.all.return_value = []
        mock_session.execute = AsyncMock(return_value=rows_result)
        mock_ctx = AsyncMock()
        mock_ctx.__aenter__ = AsyncMock(return_value=mock_session)
        mock_ctx.__aexit__ = AsyncMock(return_value=False)

        with patch("agent_trust.tools.agents.get_session", return_value=mock_ctx):
            result = await search_agents(min_score=0.99)

        assert result["total"] == 0
        assert result["agents"] == []

    @pytest.mark.asyncio
    async def test_search_agents_limit_clamped(self):
        """Limit is clamped to max 100."""
        mock_session = AsyncMock()
        rows_result = MagicMock()
        rows_result.all.return_value = []
        mock_session.execute = AsyncMock(return_value=rows_result)
        mock_ctx = AsyncMock()
        mock_ctx.__aenter__ = AsyncMock(return_value=mock_session)
        mock_ctx.__aexit__ = AsyncMock(return_value=False)

        with patch("agent_trust.tools.agents.get_session", return_value=mock_ctx):
            result = await search_agents(limit=9999)

        assert result["filters"]["limit"] == 100
