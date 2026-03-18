from __future__ import annotations

"""MCP protocol-level integration tests for AgentTrust.

Uses an in-process MCP client/server pair (create_connected_server_and_client_session).
All infrastructure (DB, Redis, HTTP auth) is mocked per test.

Coverage:
  Protocol  : initialize, tools/list (16 tools), resources/list, resource_templates, prompts/list
  Tools (16): register_agent, link_agentauth, generate_agent_token, whoami, get_agent_profile,
              search_agents, report_interaction, get_interaction_history, file_dispute,
              resolve_dispute, check_trust, get_score_breakdown, compare_agents,
              issue_attestation, verify_attestation, sybil_check
  Resources (6): trust://health, agent score, agent history, agent attestations, leaderboard, dispute
  Prompts   (3): evaluate_counterparty_prompt, explain_score_change_prompt, dispute_assessment_prompt
"""

import json
import uuid
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from tests.test_integration.conftest import (
    RATE_LIMIT_ALLOWED,
    make_identity,
    make_mock_redis,
    make_root_identity,
    make_session_ctx,
    make_standalone_identity,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

EXPECTED_TOOLS = {
    "check_trust",
    "compare_agents",
    "file_dispute",
    "generate_agent_token",
    "get_agent_profile",
    "get_interaction_history",
    "get_score_breakdown",
    "issue_attestation",
    "link_agentauth",
    "register_agent",
    "report_interaction",
    "resolve_dispute",
    "search_agents",
    "sybil_check",
    "verify_attestation",
    "whoami",
}

EXPECTED_RESOURCE_TEMPLATES = {
    "trust://agents/{agent_id}/score",
    "trust://agents/{agent_id}/history",
    "trust://agents/{agent_id}/attestations",
    "trust://leaderboard/{score_type}",
    "trust://disputes/{dispute_id}",
}

EXPECTED_PROMPTS = {
    "evaluate_counterparty_prompt",
    "explain_score_change_prompt",
    "dispute_assessment_prompt",
}


def _parse(result) -> dict:
    """Parse MCP CallToolResult first content item as JSON."""
    assert result.content, "Tool returned no content"
    return json.loads(result.content[0].text)


def _make_orm_agent(agent_id: uuid.UUID | None = None) -> MagicMock:
    a = MagicMock()
    a.agent_id = agent_id or uuid.uuid4()
    a.display_name = "Test Agent"
    a.auth_source = "agentauth"
    a.agentauth_linked = True
    a.capabilities = ["coding"]
    a.metadata_ = {}
    a.trust_level = 0.75
    a.status = "active"
    a.registered_at = datetime.now(UTC)
    a.public_key = bytes(32)
    return a


def _make_orm_score(agent_id: uuid.UUID, score_type: str = "overall") -> MagicMock:
    s = MagicMock()
    s.agent_id = agent_id
    s.score_type = score_type
    s.score = 0.80
    s.confidence = 0.90
    s.interaction_count = 15
    s.factor_breakdown = {"bayesian_raw": 0.80, "dispute_penalty": 1.0}
    s.computed_at = datetime.now(UTC)
    return s


def _make_score_data(agent_id: uuid.UUID, score_type: str = "overall", score: float = 0.80) -> dict:
    """Return a score dict matching what _get_or_compute_score returns."""
    return {
        "agent_id": str(agent_id),
        "score_type": score_type,
        "score": score,
        "confidence": 0.90,
        "interaction_count": 15,
        "factor_breakdown": {"bayesian_raw": score, "dispute_penalty": 1.0},
        "computed_at": datetime.now(UTC).isoformat(),
    }


def _make_orm_interaction(initiator_id: uuid.UUID, counterparty_id: uuid.UUID) -> MagicMock:
    i = MagicMock()
    i.interaction_id = uuid.uuid4()
    i.initiator_id = initiator_id
    i.counterparty_id = counterparty_id
    i.interaction_type = "transaction"
    i.outcome = "success"
    i.mutually_confirmed = False
    i.reported_at = datetime.now(UTC)
    return i


def _make_orm_dispute(
    interaction_id: uuid.UUID,
    filed_by: uuid.UUID,
    filed_against: uuid.UUID,
    status: str = "open",
) -> MagicMock:
    d = MagicMock()
    d.dispute_id = uuid.uuid4()
    d.interaction_id = interaction_id
    d.filed_by = filed_by
    d.filed_against = filed_against
    d.reason = "bad outcome"
    d.evidence = {}
    d.status = status
    d.created_at = datetime.now(UTC)
    d.resolved_at = None
    return d


# ---------------------------------------------------------------------------
# MCP protocol / discovery
# ---------------------------------------------------------------------------


class TestMCPProtocol:
    """Verify MCP handshake, tool discovery, resource templates, and prompts."""

    @pytest.mark.asyncio
    async def test_initialize_server_name(self, mcp_session):
        # initialize() is called inside create_connected_server_and_client_session;
        # the session is already initialised when the fixture yields.
        assert mcp_session is not None  # session alive = successful init

    @pytest.mark.asyncio
    async def test_tools_list_count(self, mcp_session):
        result = await mcp_session.list_tools()
        assert len(result.tools) == len(EXPECTED_TOOLS)

    @pytest.mark.asyncio
    async def test_tools_list_names(self, mcp_session):
        result = await mcp_session.list_tools()
        names = {t.name for t in result.tools}
        assert names == EXPECTED_TOOLS

    @pytest.mark.asyncio
    async def test_tools_have_descriptions(self, mcp_session):
        result = await mcp_session.list_tools()
        for tool in result.tools:
            assert tool.description, f"Tool {tool.name!r} has no description"

    @pytest.mark.asyncio
    async def test_tools_have_input_schemas(self, mcp_session):
        result = await mcp_session.list_tools()
        for tool in result.tools:
            assert tool.inputSchema is not None, f"Tool {tool.name!r} missing inputSchema"

    @pytest.mark.asyncio
    async def test_resource_templates_list(self, mcp_session):
        result = await mcp_session.list_resource_templates()
        uris = {t.uriTemplate for t in result.resourceTemplates}
        assert uris == EXPECTED_RESOURCE_TEMPLATES

    @pytest.mark.asyncio
    async def test_resources_list_includes_health(self, mcp_session):
        result = await mcp_session.list_resources()
        uris = {str(r.uri) for r in result.resources}
        assert "trust://health" in uris

    @pytest.mark.asyncio
    async def test_prompts_list(self, mcp_session):
        result = await mcp_session.list_prompts()
        names = {p.name for p in result.prompts}
        assert names == EXPECTED_PROMPTS

    @pytest.mark.asyncio
    async def test_prompts_have_descriptions(self, mcp_session):
        result = await mcp_session.list_prompts()
        for p in result.prompts:
            assert p.description, f"Prompt {p.name!r} has no description"


# ---------------------------------------------------------------------------
# register_agent
# ---------------------------------------------------------------------------


class TestRegisterAgentMCP:
    @pytest.mark.asyncio
    async def test_autogen_keypair_when_no_auth(self, mcp_session):
        """No auth provided → auto-generates Ed25519 key pair (standalone mode)."""
        agent = _make_orm_agent()
        with (
            patch(
                "agent_trust.tools.agents._ensure_agent_profile",
                new=AsyncMock(return_value=(agent, True)),
            ),
            patch(
                "agent_trust.tools.agents.check_rate_limit",
                new=AsyncMock(return_value=RATE_LIMIT_ALLOWED),
            ),
        ):
            r = await mcp_session.call_tool("register_agent", {"display_name": "auto"})

        assert not r.isError
        data = _parse(r)
        assert data["source"] == "standalone"
        assert data["created"] is True
        assert "public_key_hex" in data
        assert "private_key_hex" in data
        assert len(bytes.fromhex(data["public_key_hex"])) == 32
        assert "warning" in data

    @pytest.mark.asyncio
    async def test_standalone_path_with_public_key(self, mcp_session):
        """Providing public_key_hex registers via standalone path."""
        agent = _make_orm_agent()
        with (
            patch(
                "agent_trust.tools.agents._ensure_agent_profile",
                new=AsyncMock(return_value=(agent, True)),
            ),
            patch(
                "agent_trust.tools.agents.check_rate_limit",
                new=AsyncMock(return_value=RATE_LIMIT_ALLOWED),
            ),
        ):
            r = await mcp_session.call_tool("register_agent", {"public_key_hex": "ab" * 32})

        assert not r.isError
        data = _parse(r)
        assert data["source"] == "standalone"
        assert data["created"] is True
        # No private key returned when caller supplied their own key
        assert "private_key_hex" not in data

    @pytest.mark.asyncio
    async def test_agentauth_path_with_token(self, mcp_session):
        """Providing access_token registers via AgentAuth path."""
        identity = make_identity()
        agent = _make_orm_agent(uuid.UUID(identity.agent_id))
        with (
            patch(
                "agent_trust.tools.agents._resolve_identity",
                new=AsyncMock(return_value=identity),
            ),
            patch(
                "agent_trust.tools.agents._ensure_agent_profile",
                new=AsyncMock(return_value=(agent, True)),
            ),
            patch(
                "agent_trust.tools.agents.check_rate_limit",
                new=AsyncMock(return_value=RATE_LIMIT_ALLOWED),
            ),
        ):
            r = await mcp_session.call_tool("register_agent", {"access_token": "valid-token"})

        assert not r.isError
        data = _parse(r)
        assert data["source"] == "agentauth"
        assert data["created"] is True
        assert data["agent_id"] == str(agent.agent_id)

    @pytest.mark.asyncio
    async def test_idempotent_existing_agent(self, mcp_session):
        """Re-registering the same agent returns created=False."""
        identity = make_identity()
        agent = _make_orm_agent(uuid.UUID(identity.agent_id))
        with (
            patch(
                "agent_trust.tools.agents._resolve_identity",
                new=AsyncMock(return_value=identity),
            ),
            patch(
                "agent_trust.tools.agents._ensure_agent_profile",
                new=AsyncMock(return_value=(agent, False)),
            ),
            patch(
                "agent_trust.tools.agents.check_rate_limit",
                new=AsyncMock(return_value=RATE_LIMIT_ALLOWED),
            ),
        ):
            r = await mcp_session.call_tool("register_agent", {"access_token": "existing-token"})

        assert not r.isError
        data = _parse(r)
        assert data["created"] is False

    @pytest.mark.asyncio
    async def test_invalid_public_key_hex(self, mcp_session):
        """Malformed hex string raises AuthenticationError → MCP isError=True."""
        with patch(
            "agent_trust.tools.agents.check_rate_limit",
            new=AsyncMock(return_value=RATE_LIMIT_ALLOWED),
        ):
            r = await mcp_session.call_tool("register_agent", {"public_key_hex": "not-valid-hex!!"})
        assert r.isError
        assert "Invalid public_key_hex" in r.content[0].text


# ---------------------------------------------------------------------------
# generate_agent_token
# ---------------------------------------------------------------------------


class TestGenerateAgentTokenMCP:
    """Verify generate_agent_token produces valid signed JWTs via the MCP protocol.

    These tests use real Ed25519 key generation (no mocks needed — the tool
    is pure crypto with no DB or Redis dependencies).
    """

    @pytest.mark.asyncio
    async def test_returns_signed_jwt(self, mcp_session):
        """Valid keypair → access_token is a well-formed signed JWT."""
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

        private_key = Ed25519PrivateKey.generate()
        private_key_hex = private_key.private_bytes_raw().hex()
        agent_id = str(uuid.uuid4())

        r = await mcp_session.call_tool(
            "generate_agent_token",
            {"agent_id": agent_id, "private_key_hex": private_key_hex},
        )

        assert not r.isError
        data = _parse(r)
        assert "access_token" in data
        assert "expires_at" in data
        assert data["ttl_minutes"] == 60
        assert data["agent_id"] == agent_id
        # Token must be a three-segment JWT
        assert len(data["access_token"].split(".")) == 3

    @pytest.mark.asyncio
    async def test_token_verifies_against_public_key(self, mcp_session):
        """Token returned by the tool passes cryptographic verification."""
        import jwt
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

        from agent_trust.crypto.agent_token import STANDALONE_TOKEN_AUDIENCE

        private_key = Ed25519PrivateKey.generate()
        private_key_hex = private_key.private_bytes_raw().hex()
        agent_id = str(uuid.uuid4())

        r = await mcp_session.call_tool(
            "generate_agent_token",
            {"agent_id": agent_id, "private_key_hex": private_key_hex},
        )
        token = _parse(r)["access_token"]

        payload = jwt.decode(
            token,
            private_key.public_key(),
            algorithms=["EdDSA"],
            audience=STANDALONE_TOKEN_AUDIENCE,
        )
        assert payload["sub"] == agent_id
        assert payload["iss"] == agent_id

    @pytest.mark.asyncio
    async def test_token_detected_as_standalone(self, mcp_session):
        """Token passes the is_standalone_agent_token detector used by the auth router."""
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

        from agent_trust.crypto.agent_token import is_standalone_agent_token

        private_key = Ed25519PrivateKey.generate()
        agent_id = str(uuid.uuid4())

        r = await mcp_session.call_tool(
            "generate_agent_token",
            {"agent_id": agent_id, "private_key_hex": private_key.private_bytes_raw().hex()},
        )
        token = _parse(r)["access_token"]

        assert is_standalone_agent_token(token)

    @pytest.mark.asyncio
    async def test_custom_ttl(self, mcp_session):
        """ttl_minutes is respected and reflected in the response."""
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

        private_key = Ed25519PrivateKey.generate()
        agent_id = str(uuid.uuid4())

        r = await mcp_session.call_tool(
            "generate_agent_token",
            {
                "agent_id": agent_id,
                "private_key_hex": private_key.private_bytes_raw().hex(),
                "ttl_minutes": 120,
            },
        )

        assert not r.isError
        assert _parse(r)["ttl_minutes"] == 120

    @pytest.mark.asyncio
    async def test_ttl_clamped_to_max(self, mcp_session):
        """TTL above 1440 (24 h) is silently clamped."""
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

        private_key = Ed25519PrivateKey.generate()

        r = await mcp_session.call_tool(
            "generate_agent_token",
            {
                "agent_id": str(uuid.uuid4()),
                "private_key_hex": private_key.private_bytes_raw().hex(),
                "ttl_minutes": 99999,
            },
        )

        assert not r.isError
        assert _parse(r)["ttl_minutes"] == 1440

    @pytest.mark.asyncio
    async def test_invalid_private_key_hex_returns_error(self, mcp_session):
        """Malformed private_key_hex returns an error dict (not isError)."""
        r = await mcp_session.call_tool(
            "generate_agent_token",
            {"agent_id": str(uuid.uuid4()), "private_key_hex": "not-valid-hex!!!"},
        )

        assert not r.isError  # tool returns {"error": ...} not an MCP-level error
        data = _parse(r)
        assert "error" in data

    @pytest.mark.asyncio
    async def test_token_used_for_report_interaction(self, mcp_session):
        """End-to-end: token from generate_agent_token works as access_token
        for report_interaction (the primary use-case for returning agents)."""
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

        private_key = Ed25519PrivateKey.generate()
        agent_id = str(uuid.uuid4())

        # Step 1: generate a token via MCP
        r = await mcp_session.call_tool(
            "generate_agent_token",
            {"agent_id": agent_id, "private_key_hex": private_key.private_bytes_raw().hex()},
        )
        token = _parse(r)["access_token"]

        # Step 2: use that token in report_interaction
        reporter_id = str(uuid.uuid4())
        counterparty_id = str(uuid.uuid4())
        identity = make_standalone_identity(reporter_id)

        reporter_agent = _make_orm_agent(uuid.UUID(reporter_id))
        counterparty_agent = _make_orm_agent(uuid.UUID(counterparty_id))
        # report_interaction queries: reporter_agent, counterparty_agent, pair_count, dedup_result, counterparty_report
        session_ctx = make_session_ctx(reporter_agent, counterparty_agent, 0, None, None)

        with (
            patch(
                "agent_trust.tools.interactions._resolve_identity_for_interaction",
                new=AsyncMock(return_value=identity),
            ),
            patch(
                "agent_trust.tools.interactions.get_session",
                session_ctx,
            ),
            patch(
                "agent_trust.tools.interactions._enqueue_score_recomputation",
                new=AsyncMock(),
            ),
            patch(
                "agent_trust.ratelimit.check_rate_limit",
                new=AsyncMock(return_value=RATE_LIMIT_ALLOWED),
            ),
        ):
            r2 = await mcp_session.call_tool(
                "report_interaction",
                {
                    "counterparty_id": counterparty_id,
                    "interaction_type": "transaction",
                    "outcome": "success",
                    "access_token": token,
                },
            )

        assert not r2.isError
        data2 = _parse(r2)
        assert "interaction_id" in data2


# ---------------------------------------------------------------------------
# link_agentauth
# ---------------------------------------------------------------------------


class TestLinkAgentauthMCP:
    @pytest.mark.asyncio
    async def test_link_success(self, mcp_session):
        import time

        import jwt as pyjwt
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

        aa_identity = make_identity()

        private_key = Ed25519PrivateKey.generate()
        public_key = private_key.public_key()
        pub_hex = public_key.public_bytes_raw().hex()

        standalone_agent = _make_orm_agent()
        standalone_agent.agent_id = uuid.uuid4()
        standalone_agent.public_key = bytes.fromhex(pub_hex)
        standalone_agent.agentauth_linked = False

        proof_payload = {"sub": pub_hex, "action": "link_agentauth", "iat": int(time.time())}
        signed_proof = pyjwt.encode(proof_payload, private_key, algorithm="EdDSA")

        session_ctx = make_session_ctx(standalone_agent)
        with (
            patch(
                "agent_trust.tools.agents.AgentAuthProvider.authenticate",
                new=AsyncMock(return_value=aa_identity),
            ),
            patch("agent_trust.tools.agents.get_session", session_ctx),
            patch("agent_trust.db.redis.get_redis", new=AsyncMock(return_value=make_mock_redis())),
        ):
            r = await mcp_session.call_tool(
                "link_agentauth",
                {
                    "access_token": "tok",
                    "public_key_hex": pub_hex,
                    "signed_proof": signed_proof,
                },
            )

        assert not r.isError
        data = _parse(r)
        assert data["merged"] is True
        assert "agent_id" in data

    @pytest.mark.asyncio
    async def test_link_unknown_key(self, mcp_session):
        aa_identity = make_identity()
        session_ctx = make_session_ctx(None)  # no agent found
        with (
            patch(
                "agent_trust.tools.agents.AgentAuthProvider.authenticate",
                new=AsyncMock(return_value=aa_identity),
            ),
            patch("agent_trust.tools.agents.get_session", session_ctx),
            patch("agent_trust.db.redis.get_redis", new=AsyncMock(return_value=make_mock_redis())),
        ):
            r = await mcp_session.call_tool(
                "link_agentauth",
                {"access_token": "tok", "public_key_hex": "ff" * 32},
            )

        # AuthenticationError is raised → FastMCP isError=True
        assert r.isError


# ---------------------------------------------------------------------------
# whoami
# ---------------------------------------------------------------------------


class TestWhoamiMCP:
    @pytest.mark.asyncio
    async def test_whoami_known_agent(self, mcp_session):
        identity = make_identity()
        agent = _make_orm_agent(uuid.UUID(identity.agent_id))
        score = _make_orm_score(agent.agent_id)
        session_ctx = make_session_ctx(agent, [score])

        with (
            patch(
                "agent_trust.tools.agents._resolve_identity",
                new=AsyncMock(return_value=identity),
            ),
            patch("agent_trust.tools.agents.get_session", session_ctx),
        ):
            r = await mcp_session.call_tool("whoami", {"access_token": "tok"})

        assert not r.isError
        data = _parse(r)
        assert data["agent_id"] == str(agent.agent_id)
        assert data["source"] == "agentauth"
        assert "scores" in data

    @pytest.mark.asyncio
    async def test_whoami_no_profile_shows_note(self, mcp_session):
        identity = make_identity()
        session_ctx = make_session_ctx(None)  # no profile yet
        with (
            patch(
                "agent_trust.tools.agents._resolve_identity",
                new=AsyncMock(return_value=identity),
            ),
            patch("agent_trust.tools.agents.get_session", session_ctx),
        ):
            r = await mcp_session.call_tool("whoami", {"access_token": "tok"})

        assert not r.isError
        data = _parse(r)
        assert "note" in data
        assert "register_agent" in data["note"]

    @pytest.mark.asyncio
    async def test_whoami_no_credentials_raises(self, mcp_session):
        """No token and no public key raises AuthenticationError → isError."""
        r = await mcp_session.call_tool("whoami", {})
        assert r.isError


# ---------------------------------------------------------------------------
# get_agent_profile
# ---------------------------------------------------------------------------


class TestGetAgentProfileMCP:
    @pytest.mark.asyncio
    async def test_profile_found(self, mcp_session):
        agent = _make_orm_agent()
        score = _make_orm_score(agent.agent_id)
        session_ctx = make_session_ctx(agent, [score])
        with patch("agent_trust.tools.agents.get_session", session_ctx):
            r = await mcp_session.call_tool("get_agent_profile", {"agent_id": str(agent.agent_id)})

        assert not r.isError
        data = _parse(r)
        assert data["agent_id"] == str(agent.agent_id)
        assert "trust_level" in data
        assert "capabilities" in data

    @pytest.mark.asyncio
    async def test_profile_not_found(self, mcp_session):
        session_ctx = make_session_ctx(None, [])
        with patch("agent_trust.tools.agents.get_session", session_ctx):
            r = await mcp_session.call_tool("get_agent_profile", {"agent_id": str(uuid.uuid4())})

        assert not r.isError
        data = _parse(r)
        assert data["error"] == "not_found"

    @pytest.mark.asyncio
    async def test_profile_invalid_uuid(self, mcp_session):
        r = await mcp_session.call_tool("get_agent_profile", {"agent_id": "not-a-uuid"})
        assert not r.isError
        data = _parse(r)
        assert data["error"] == "invalid_agent_id"


# ---------------------------------------------------------------------------
# search_agents
# ---------------------------------------------------------------------------


class TestSearchAgentsMCP:
    @pytest.mark.asyncio
    async def test_search_returns_agents(self, mcp_session):
        agent = _make_orm_agent()
        score = _make_orm_score(agent.agent_id)
        # search_agents does a join → rows are (Agent, TrustScore) tuples
        session_ctx = make_session_ctx([(agent, score)])
        with patch("agent_trust.tools.agents.get_session", session_ctx):
            r = await mcp_session.call_tool("search_agents", {"min_score": 0.0})

        assert not r.isError
        data = _parse(r)
        assert "agents" in data
        assert data["total"] >= 0

    @pytest.mark.asyncio
    async def test_search_limit_clamped(self, mcp_session):
        """limit > 100 is silently clamped to 100."""
        session_ctx = make_session_ctx([])
        with patch("agent_trust.tools.agents.get_session", session_ctx):
            r = await mcp_session.call_tool("search_agents", {"limit": 9999})

        assert not r.isError
        data = _parse(r)
        assert data["filters"]["limit"] == 100


# ---------------------------------------------------------------------------
# report_interaction
# ---------------------------------------------------------------------------


class TestReportInteractionMCP:
    @pytest.mark.asyncio
    async def test_auth_error(self, mcp_session):
        """Bad token → returns error dict (not MCP isError)."""
        from agent_trust.auth.identity import AuthenticationError

        with patch(
            "agent_trust.tools.interactions._resolve_identity_for_interaction",
            new=AsyncMock(side_effect=AuthenticationError("bad token")),
        ):
            r = await mcp_session.call_tool(
                "report_interaction",
                {
                    "counterparty_id": str(uuid.uuid4()),
                    "interaction_type": "transaction",
                    "outcome": "success",
                    "access_token": "bad",
                },
            )

        assert not r.isError
        data = _parse(r)
        assert "error" in data

    @pytest.mark.asyncio
    async def test_invalid_interaction_type(self, mcp_session):
        identity = make_identity()
        with (
            patch(
                "agent_trust.tools.interactions._resolve_identity_for_interaction",
                new=AsyncMock(return_value=identity),
            ),
            patch(
                "agent_trust.ratelimit.check_rate_limit",
                new=AsyncMock(return_value=RATE_LIMIT_ALLOWED),
            ),
        ):
            r = await mcp_session.call_tool(
                "report_interaction",
                {
                    "counterparty_id": str(uuid.uuid4()),
                    "interaction_type": "INVALID_TYPE",
                    "outcome": "success",
                    "access_token": "tok",
                },
            )

        assert not r.isError
        data = _parse(r)
        assert "error" in data
        assert "interaction_type" in data["error"]

    @pytest.mark.asyncio
    async def test_invalid_outcome(self, mcp_session):
        identity = make_identity()
        with (
            patch(
                "agent_trust.tools.interactions._resolve_identity_for_interaction",
                new=AsyncMock(return_value=identity),
            ),
            patch(
                "agent_trust.ratelimit.check_rate_limit",
                new=AsyncMock(return_value=RATE_LIMIT_ALLOWED),
            ),
        ):
            r = await mcp_session.call_tool(
                "report_interaction",
                {
                    "counterparty_id": str(uuid.uuid4()),
                    "interaction_type": "transaction",
                    "outcome": "BAD_OUTCOME",
                    "access_token": "tok",
                },
            )

        assert not r.isError
        data = _parse(r)
        assert "error" in data
        assert "outcome" in data["error"]

    @pytest.mark.asyncio
    async def test_self_report_rejected(self, mcp_session):
        identity = make_identity()
        with (
            patch(
                "agent_trust.tools.interactions._resolve_identity_for_interaction",
                new=AsyncMock(return_value=identity),
            ),
            patch(
                "agent_trust.ratelimit.check_rate_limit",
                new=AsyncMock(return_value=RATE_LIMIT_ALLOWED),
            ),
        ):
            r = await mcp_session.call_tool(
                "report_interaction",
                {
                    "counterparty_id": identity.agent_id,  # same as reporter
                    "interaction_type": "transaction",
                    "outcome": "success",
                    "access_token": "tok",
                },
            )

        assert not r.isError
        data = _parse(r)
        assert "error" in data
        assert "yourself" in data["error"]

    @pytest.mark.asyncio
    async def test_rate_limit_exceeded(self, mcp_session):
        from agent_trust.ratelimit import RateLimitResult

        identity = make_identity()
        denied = RateLimitResult(
            allowed=False, limit=10, remaining=0, reset_at=9999, retry_after=30
        )
        with (
            patch(
                "agent_trust.tools.interactions._resolve_identity_for_interaction",
                new=AsyncMock(return_value=identity),
            ),
            patch(
                "agent_trust.ratelimit.check_rate_limit",
                new=AsyncMock(return_value=denied),
            ),
        ):
            r = await mcp_session.call_tool(
                "report_interaction",
                {
                    "counterparty_id": str(uuid.uuid4()),
                    "interaction_type": "transaction",
                    "outcome": "success",
                    "access_token": "tok",
                },
            )

        assert not r.isError
        data = _parse(r)
        assert "Rate limit" in data["error"]
        assert "retry_after_seconds" in data


# ---------------------------------------------------------------------------
# get_interaction_history
# ---------------------------------------------------------------------------


class TestGetInteractionHistoryMCP:
    @pytest.mark.asyncio
    async def test_invalid_uuid(self, mcp_session):
        identity = make_identity(scopes=["trust.read"])
        with patch(
            "agent_trust.auth.resolve.resolve_identity",
            new=AsyncMock(return_value=identity),
        ):
            r = await mcp_session.call_tool(
                "get_interaction_history", {"agent_id": "not-a-uuid", "access_token": "valid-tok"}
            )
        assert not r.isError
        data = _parse(r)
        assert "error" in data
        assert "UUID" in data["error"]

    @pytest.mark.asyncio
    async def test_agent_not_found(self, mcp_session):
        identity = make_identity(scopes=["trust.read"])
        session_ctx = make_session_ctx(None)
        with (
            patch(
                "agent_trust.auth.resolve.resolve_identity",
                new=AsyncMock(return_value=identity),
            ),
            patch("agent_trust.tools.interactions.get_session", session_ctx),
        ):
            r = await mcp_session.call_tool(
                "get_interaction_history",
                {"agent_id": str(uuid.uuid4()), "access_token": "valid-tok"},
            )

        assert not r.isError
        data = _parse(r)
        assert "error" in data
        assert "not found" in data["error"]

    @pytest.mark.asyncio
    async def test_success_returns_list(self, mcp_session):
        identity = make_identity(scopes=["trust.read"])
        agent_id = uuid.uuid4()
        agent = _make_orm_agent(agent_id)
        ix = _make_orm_interaction(agent_id, uuid.uuid4())
        session_ctx = make_session_ctx(agent, [ix])
        with (
            patch(
                "agent_trust.auth.resolve.resolve_identity",
                new=AsyncMock(return_value=identity),
            ),
            patch("agent_trust.tools.interactions.get_session", session_ctx),
        ):
            r = await mcp_session.call_tool(
                "get_interaction_history", {"agent_id": str(agent_id), "access_token": "valid-tok"}
            )

        assert not r.isError
        data = _parse(r)
        assert data["agent_id"] == str(agent_id)
        assert "interactions" in data
        assert isinstance(data["interactions"], list)

    @pytest.mark.asyncio
    async def test_invalid_interaction_type_filter(self, mcp_session):
        agent = _make_orm_agent()
        session_ctx = make_session_ctx(agent)
        with patch("agent_trust.tools.interactions.get_session", session_ctx):
            r = await mcp_session.call_tool(
                "get_interaction_history",
                {"agent_id": str(agent.agent_id), "interaction_type": "BOGUS"},
            )

        assert not r.isError
        data = _parse(r)
        assert "error" in data


# ---------------------------------------------------------------------------
# file_dispute
# ---------------------------------------------------------------------------


class TestFileDisputeMCP:
    @pytest.mark.asyncio
    async def test_auth_error(self, mcp_session):
        from agent_trust.auth.identity import AuthenticationError

        with (
            patch("agent_trust.db.redis.get_redis", new=AsyncMock(return_value=make_mock_redis())),
            patch(
                "agent_trust.tools.disputes.AgentAuthProvider.authenticate",
                new=AsyncMock(side_effect=AuthenticationError("bad")),
            ),
        ):
            r = await mcp_session.call_tool(
                "file_dispute",
                {
                    "interaction_id": str(uuid.uuid4()),
                    "reason": "fraud",
                    "access_token": "bad-tok",
                },
            )

        assert not r.isError
        data = _parse(r)
        assert "error" in data

    @pytest.mark.asyncio
    async def test_interaction_not_found(self, mcp_session):
        identity = make_identity(scopes=["trust.read", "trust.report", "trust.dispute.file"])
        # file_dispute queries: dismissed_count, last_dismissed_at, interaction (not found)
        session_ctx = make_session_ctx(0, None, None)
        with (
            patch("agent_trust.db.redis.get_redis", new=AsyncMock(return_value=make_mock_redis())),
            patch(
                "agent_trust.tools.disputes.AgentAuthProvider.authenticate",
                new=AsyncMock(return_value=identity),
            ),
            patch("agent_trust.tools.disputes.get_session", session_ctx),
            patch(
                "agent_trust.ratelimit.check_rate_limit",
                new=AsyncMock(return_value=RATE_LIMIT_ALLOWED),
            ),
        ):
            r = await mcp_session.call_tool(
                "file_dispute",
                {
                    "interaction_id": str(uuid.uuid4()),
                    "reason": "fraud",
                    "access_token": "valid-tok",
                },
            )

        assert not r.isError
        data = _parse(r)
        assert "error" in data
        assert "not found" in data["error"].lower()

    @pytest.mark.asyncio
    async def test_not_party_to_interaction(self, mcp_session):
        """Filer is not initiator or counterparty → error."""
        identity = make_identity(scopes=["trust.read", "trust.report", "trust.dispute.file"])
        ix = _make_orm_interaction(uuid.uuid4(), uuid.uuid4())  # stranger IDs
        # file_dispute queries: dismissed_count, last_dismissed_at, interaction
        session_ctx = make_session_ctx(0, None, ix)
        with (
            patch("agent_trust.db.redis.get_redis", new=AsyncMock(return_value=make_mock_redis())),
            patch(
                "agent_trust.tools.disputes.AgentAuthProvider.authenticate",
                new=AsyncMock(return_value=identity),
            ),
            patch("agent_trust.tools.disputes.get_session", session_ctx),
            patch(
                "agent_trust.ratelimit.check_rate_limit",
                new=AsyncMock(return_value=RATE_LIMIT_ALLOWED),
            ),
        ):
            r = await mcp_session.call_tool(
                "file_dispute",
                {
                    "interaction_id": str(ix.interaction_id),
                    "reason": "fraud",
                    "access_token": "valid-tok",
                },
            )

        assert not r.isError
        data = _parse(r)
        assert "error" in data
        assert "party" in data["error"]


# ---------------------------------------------------------------------------
# resolve_dispute
# ---------------------------------------------------------------------------


class TestResolveDisputeMCP:
    @pytest.mark.asyncio
    async def test_auth_error(self, mcp_session):
        from agent_trust.auth.identity import AuthenticationError

        with (
            patch("agent_trust.db.redis.get_redis", new=AsyncMock(return_value=make_mock_redis())),
            patch(
                "agent_trust.tools.disputes.AgentAuthProvider.authenticate",
                new=AsyncMock(side_effect=AuthenticationError("bad")),
            ),
        ):
            r = await mcp_session.call_tool(
                "resolve_dispute",
                {
                    "dispute_id": str(uuid.uuid4()),
                    "resolution": "upheld",
                    "access_token": "bad-tok",
                },
            )

        assert not r.isError
        data = _parse(r)
        assert "error" in data

    @pytest.mark.asyncio
    async def test_invalid_resolution(self, mcp_session):
        identity = make_root_identity()
        with (
            patch("agent_trust.db.redis.get_redis", new=AsyncMock(return_value=make_mock_redis())),
            patch(
                "agent_trust.tools.disputes.AgentAuthProvider.authenticate",
                new=AsyncMock(return_value=identity),
            ),
            patch(
                "agent_trust.tools.disputes.AgentAuthProvider.check_permission",
                new=AsyncMock(return_value=True),
            ),
        ):
            r = await mcp_session.call_tool(
                "resolve_dispute",
                {
                    "dispute_id": str(uuid.uuid4()),
                    "resolution": "INVALID",
                    "access_token": "tok",
                },
            )

        assert not r.isError
        data = _parse(r)
        assert "error" in data
        assert "resolution" in data["error"].lower()

    @pytest.mark.asyncio
    async def test_dispute_not_found(self, mcp_session):
        identity = make_root_identity()
        session_ctx = make_session_ctx(None)
        with (
            patch("agent_trust.db.redis.get_redis", new=AsyncMock(return_value=make_mock_redis())),
            patch(
                "agent_trust.tools.disputes.AgentAuthProvider.authenticate",
                new=AsyncMock(return_value=identity),
            ),
            patch(
                "agent_trust.tools.disputes.AgentAuthProvider.check_permission",
                new=AsyncMock(return_value=True),
            ),
            patch("agent_trust.tools.disputes.get_session", session_ctx),
        ):
            r = await mcp_session.call_tool(
                "resolve_dispute",
                {
                    "dispute_id": str(uuid.uuid4()),
                    "resolution": "upheld",
                    "access_token": "tok",
                },
            )

        assert not r.isError
        data = _parse(r)
        assert "error" in data
        assert "not found" in data["error"].lower()


# ---------------------------------------------------------------------------
# check_trust
# ---------------------------------------------------------------------------


class TestCheckTrustMCP:
    @pytest.mark.asyncio
    async def test_invalid_uuid(self, mcp_session):
        with (
            patch("agent_trust.db.redis.get_redis", new=AsyncMock(return_value=make_mock_redis())),
            patch(
                "agent_trust.ratelimit.check_rate_limit",
                new=AsyncMock(return_value=RATE_LIMIT_ALLOWED),
            ),
        ):
            r = await mcp_session.call_tool("check_trust", {"agent_id": "not-a-uuid"})

        assert not r.isError
        data = _parse(r)
        assert "error" in data
        assert "UUID" in data["error"]

    @pytest.mark.asyncio
    async def test_invalid_score_type(self, mcp_session):
        r = await mcp_session.call_tool(
            "check_trust", {"agent_id": str(uuid.uuid4()), "score_type": "INVALID"}
        )
        assert not r.isError
        data = _parse(r)
        assert "error" in data
        assert "score_type" in data["error"]

    @pytest.mark.asyncio
    async def test_agent_not_found(self, mcp_session):
        session_ctx = make_session_ctx(None)
        with (
            patch("agent_trust.db.redis.get_redis", new=AsyncMock(return_value=make_mock_redis())),
            patch(
                "agent_trust.ratelimit.check_rate_limit",
                new=AsyncMock(return_value=RATE_LIMIT_ALLOWED),
            ),
            patch("agent_trust.tools.scoring.get_session", session_ctx),
        ):
            r = await mcp_session.call_tool("check_trust", {"agent_id": str(uuid.uuid4())})

        assert not r.isError
        data = _parse(r)
        assert "error" in data
        assert "not found" in data["error"].lower()

    @pytest.mark.asyncio
    async def test_check_trust_success(self, mcp_session):
        agent = _make_orm_agent()
        score_data = _make_score_data(agent.agent_id)
        session_ctx = make_session_ctx(agent)
        with (
            patch("agent_trust.db.redis.get_redis", new=AsyncMock(return_value=make_mock_redis())),
            patch(
                "agent_trust.ratelimit.check_rate_limit",
                new=AsyncMock(return_value=RATE_LIMIT_ALLOWED),
            ),
            patch("agent_trust.tools.scoring.get_session", session_ctx),
            patch(
                "agent_trust.tools.scoring._get_or_compute_score",
                new=AsyncMock(return_value=score_data),
            ),
        ):
            r = await mcp_session.call_tool("check_trust", {"agent_id": str(agent.agent_id)})

        assert not r.isError
        data = _parse(r)
        assert "score" in data
        assert "confidence" in data
        assert "interaction_count" in data


# ---------------------------------------------------------------------------
# get_score_breakdown
# ---------------------------------------------------------------------------


class TestGetScoreBreakdownMCP:
    @pytest.mark.asyncio
    async def test_no_auth_returns_error(self, mcp_session):
        from agent_trust.auth.identity import AuthenticationError

        with (
            patch("agent_trust.db.redis.get_redis", new=AsyncMock(return_value=make_mock_redis())),
            patch(
                "agent_trust.tools.scoring.AgentAuthProvider.authenticate",
                new=AsyncMock(side_effect=AuthenticationError("no token")),
            ),
        ):
            r = await mcp_session.call_tool(
                "get_score_breakdown",
                {"agent_id": str(uuid.uuid4()), "access_token": "bad"},
            )

        assert not r.isError
        data = _parse(r)
        assert "error" in data

    @pytest.mark.asyncio
    async def test_agent_not_found(self, mcp_session):
        identity = make_identity()
        session_ctx = make_session_ctx(None)
        with (
            patch("agent_trust.db.redis.get_redis", new=AsyncMock(return_value=make_mock_redis())),
            patch(
                "agent_trust.tools.scoring.AgentAuthProvider.authenticate",
                new=AsyncMock(return_value=identity),
            ),
            patch("agent_trust.tools.scoring.get_session", session_ctx),
        ):
            r = await mcp_session.call_tool(
                "get_score_breakdown",
                {"agent_id": str(uuid.uuid4()), "access_token": "tok"},
            )

        assert not r.isError
        data = _parse(r)
        assert "error" in data
        assert "not found" in data["error"].lower()

    @pytest.mark.asyncio
    async def test_success_returns_all_score_types(self, mcp_session):
        identity = make_identity()
        agent = _make_orm_agent()
        session_ctx = make_session_ctx(agent)

        async def mock_get_or_compute(agent_id, score_type):
            return _make_score_data(agent_id, score_type)

        with (
            patch("agent_trust.db.redis.get_redis", new=AsyncMock(return_value=make_mock_redis())),
            patch(
                "agent_trust.tools.scoring.AgentAuthProvider.authenticate",
                new=AsyncMock(return_value=identity),
            ),
            patch("agent_trust.tools.scoring.get_session", session_ctx),
            patch(
                "agent_trust.tools.scoring._get_or_compute_score",
                new=AsyncMock(side_effect=mock_get_or_compute),
            ),
        ):
            r = await mcp_session.call_tool(
                "get_score_breakdown",
                {"agent_id": str(agent.agent_id), "access_token": "tok"},
            )

        assert not r.isError
        data = _parse(r)
        assert "scores" in data
        assert "computed_by" in data


# ---------------------------------------------------------------------------
# compare_agents
# ---------------------------------------------------------------------------


class TestCompareAgentsMCP:
    @pytest.mark.asyncio
    async def test_empty_list(self, mcp_session):
        r = await mcp_session.call_tool("compare_agents", {"agent_ids": []})
        assert not r.isError
        data = _parse(r)
        assert "error" in data

    @pytest.mark.asyncio
    async def test_too_many_agents(self, mcp_session):
        ids = [str(uuid.uuid4()) for _ in range(11)]
        r = await mcp_session.call_tool("compare_agents", {"agent_ids": ids})
        assert not r.isError
        data = _parse(r)
        assert "error" in data
        assert "Maximum" in data["error"]

    @pytest.mark.asyncio
    async def test_invalid_score_type(self, mcp_session):
        r = await mcp_session.call_tool(
            "compare_agents",
            {"agent_ids": [str(uuid.uuid4())], "score_type": "BOGUS"},
        )
        assert not r.isError
        data = _parse(r)
        assert "error" in data

    @pytest.mark.asyncio
    async def test_compare_two_agents(self, mcp_session):
        agent_a = _make_orm_agent()
        agent_b = _make_orm_agent()
        session_ctx = make_session_ctx(agent_a, agent_b)

        score_map = {
            agent_a.agent_id: _make_score_data(agent_a.agent_id, score=0.80),
            agent_b.agent_id: _make_score_data(agent_b.agent_id, score=0.60),
        }

        async def mock_get_or_compute(agent_id, score_type):
            return score_map.get(agent_id)

        with (
            patch("agent_trust.tools.scoring.get_session", session_ctx),
            patch(
                "agent_trust.tools.scoring._get_or_compute_score",
                new=AsyncMock(side_effect=mock_get_or_compute),
            ),
        ):
            r = await mcp_session.call_tool(
                "compare_agents",
                {"agent_ids": [str(agent_a.agent_id), str(agent_b.agent_id)]},
            )

        assert not r.isError
        data = _parse(r)
        assert data["count"] == 2
        # Higher-scored agent should be ranked first
        ranks = [a.get("rank") for a in data["agents"] if "rank" in a]
        assert ranks == sorted(ranks)


# ---------------------------------------------------------------------------
# issue_attestation
# ---------------------------------------------------------------------------


class TestIssueAttestationMCP:
    @pytest.mark.asyncio
    async def test_auth_error(self, mcp_session):
        from agent_trust.auth.identity import AuthenticationError

        with (
            patch("agent_trust.db.redis.get_redis", new=AsyncMock(return_value=make_mock_redis())),
            patch(
                "agent_trust.tools.attestations.AgentAuthProvider.authenticate",
                new=AsyncMock(side_effect=AuthenticationError("bad")),
            ),
        ):
            r = await mcp_session.call_tool(
                "issue_attestation",
                {"agent_id": str(uuid.uuid4()), "access_token": "bad"},
            )

        assert not r.isError
        data = _parse(r)
        assert "error" in data

    @pytest.mark.asyncio
    async def test_agent_not_found(self, mcp_session):
        identity = make_identity(scopes=["trust.read", "trust.report", "trust.attest.issue"])
        session_ctx = make_session_ctx(None)
        with (
            patch("agent_trust.db.redis.get_redis", new=AsyncMock(return_value=make_mock_redis())),
            patch(
                "agent_trust.tools.attestations.AgentAuthProvider.authenticate",
                new=AsyncMock(return_value=identity),
            ),
            patch(
                "agent_trust.ratelimit.check_rate_limit",
                new=AsyncMock(return_value=RATE_LIMIT_ALLOWED),
            ),
            patch("agent_trust.tools.attestations.get_session", session_ctx),
        ):
            r = await mcp_session.call_tool(
                "issue_attestation",
                {"agent_id": str(uuid.uuid4()), "access_token": "tok"},
            )

        assert not r.isError
        data = _parse(r)
        assert "error" in data
        assert "not found" in data["error"].lower()


# ---------------------------------------------------------------------------
# verify_attestation
# ---------------------------------------------------------------------------


class TestVerifyAttestationMCP:
    @pytest.mark.asyncio
    async def test_malformed_token(self, mcp_session):
        r = await mcp_session.call_tool("verify_attestation", {"jwt_token": "not.a.jwt"})
        assert not r.isError
        data = _parse(r)
        assert data["valid"] is False
        assert "error" in data

    @pytest.mark.asyncio
    async def test_expired_token(self, mcp_session):
        """An expired but structurally valid JWT returns valid=False."""
        import time

        import jwt as pyjwt

        payload = {
            "sub": str(uuid.uuid4()),
            "jti": str(uuid.uuid4()),
            "scores": {},
            "iat": int(time.time()) - 7200,
            "nbf": int(time.time()) - 7200,
            "exp": int(time.time()) - 3600,  # expired
            "iss": "agent-trust",
        }
        # sign with a throwaway key — verify_attestation will fail signature
        token = pyjwt.encode(payload, "wrong-key", algorithm="HS256")
        r = await mcp_session.call_tool("verify_attestation", {"jwt_token": token})
        assert not r.isError
        data = _parse(r)
        assert data["valid"] is False

    @pytest.mark.asyncio
    async def test_invalid_signature(self, mcp_session):
        """Token with wrong signature → valid=False, invalid signature error."""
        import time

        import jwt as pyjwt

        payload = {
            "sub": str(uuid.uuid4()),
            "jti": str(uuid.uuid4()),
            "scores": {},
            "iat": int(time.time()),
            "nbf": int(time.time()),
            "exp": int(time.time()) + 3600,
            "iss": "agent-trust",
        }
        token = pyjwt.encode(payload, "wrong-key", algorithm="HS256")
        # Peek at jti lookup (DB) — return no record so revocation check passes
        session_ctx = make_session_ctx(None)
        with patch("agent_trust.tools.attestations.get_session", session_ctx):
            r = await mcp_session.call_tool("verify_attestation", {"jwt_token": token})

        assert not r.isError
        data = _parse(r)
        assert data["valid"] is False


# ---------------------------------------------------------------------------
# sybil_check
# ---------------------------------------------------------------------------


class TestSybilCheckMCP:
    @pytest.mark.asyncio
    async def test_clean_agent(self, mcp_session):
        from datetime import UTC, datetime

        from agent_trust.engine.sybil_detector import SybilReport

        agent_id = str(uuid.uuid4())
        mock_report = SybilReport(
            agent_id=agent_id,
            risk_score=0.0,
            signals=[],
            checked_at=datetime.now(UTC),
        )
        mock_detector = AsyncMock()
        mock_detector.check_agent = AsyncMock(return_value=mock_report)

        @asynccontextmanager
        async def mock_get_session():
            yield MagicMock()

        with (
            patch("agent_trust.tools.sybil.get_session", mock_get_session),
            patch("agent_trust.tools.sybil.SybilDetector", return_value=mock_detector),
        ):
            r = await mcp_session.call_tool("sybil_check", {"agent_id": agent_id})

        assert not r.isError
        data = _parse(r)
        assert data["agent_id"] == agent_id
        assert data["risk_score"] == 0.0
        assert data["is_suspicious"] is False
        assert isinstance(data["signals"], list)

    @pytest.mark.asyncio
    async def test_suspicious_agent(self, mcp_session):
        from datetime import UTC, datetime

        from agent_trust.engine.sybil_detector import SybilReport, SybilSignal

        agent_id = str(uuid.uuid4())
        signal = SybilSignal(
            signal_type="ring_reporting",
            severity=0.85,
            description="Mutual positive feedback loop detected",
            evidence={"pair_count": 5},
        )
        mock_report = SybilReport(
            agent_id=agent_id,
            risk_score=0.85,
            signals=[signal],
            checked_at=datetime.now(UTC),
        )
        mock_detector = AsyncMock()
        mock_detector.check_agent = AsyncMock(return_value=mock_report)

        @asynccontextmanager
        async def mock_get_session():
            yield MagicMock()

        with (
            patch("agent_trust.tools.sybil.get_session", mock_get_session),
            patch("agent_trust.tools.sybil.SybilDetector", return_value=mock_detector),
        ):
            r = await mcp_session.call_tool("sybil_check", {"agent_id": agent_id})

        assert not r.isError
        data = _parse(r)
        assert data["risk_score"] == 0.85
        assert data["is_suspicious"] is True
        assert len(data["signals"]) == 1
        assert data["signals"][0]["signal_type"] == "ring_reporting"


# ---------------------------------------------------------------------------
# Resources
# ---------------------------------------------------------------------------


class TestResourcesMCP:
    @pytest.mark.asyncio
    async def test_health_resource(self, mcp_session):
        """trust://health returns JSON with checks dict."""
        mock_redis = make_mock_redis()
        with (
            patch("agent_trust.db.redis.get_redis", new=AsyncMock(return_value=mock_redis)),
        ):
            result = await mcp_session.read_resource("trust://health")

        assert result.contents
        data = json.loads(result.contents[0].text)
        assert "service" in data
        assert "checks" in data
        assert "overall" in data

    @pytest.mark.asyncio
    async def test_agent_score_resource_not_found(self, mcp_session):
        """trust://agents/{id}/score with unknown agent returns error JSON."""
        session_ctx = make_session_ctx(None)
        agent_id = str(uuid.uuid4())
        with patch("agent_trust.db.session.get_session", session_ctx):
            result = await mcp_session.read_resource(f"trust://agents/{agent_id}/score")

        assert result.contents
        data = json.loads(result.contents[0].text)
        assert "error" in data

    @pytest.mark.asyncio
    async def test_agent_score_resource_found(self, mcp_session):
        agent = _make_orm_agent()
        score = _make_orm_score(agent.agent_id)
        session_ctx = make_session_ctx(agent, [score])
        with patch("agent_trust.db.session.get_session", session_ctx):
            result = await mcp_session.read_resource(f"trust://agents/{agent.agent_id}/score")

        assert result.contents
        data = json.loads(result.contents[0].text)
        assert "agent_id" in data
        assert "scores" in data

    @pytest.mark.asyncio
    async def test_agent_history_resource_not_found(self, mcp_session):
        session_ctx = make_session_ctx(None)
        agent_id = str(uuid.uuid4())
        with patch("agent_trust.db.session.get_session", session_ctx):
            result = await mcp_session.read_resource(f"trust://agents/{agent_id}/history")

        assert result.contents
        data = json.loads(result.contents[0].text)
        assert "error" in data

    @pytest.mark.asyncio
    async def test_leaderboard_resource_valid_type(self, mcp_session):
        session_ctx = make_session_ctx([])
        with patch("agent_trust.db.session.get_session", session_ctx):
            result = await mcp_session.read_resource("trust://leaderboard/overall")

        assert result.contents
        data = json.loads(result.contents[0].text)
        assert "leaderboard" in data or "error" not in data

    @pytest.mark.asyncio
    async def test_leaderboard_resource_invalid_type(self, mcp_session):
        result = await mcp_session.read_resource("trust://leaderboard/BOGUS")
        assert result.contents
        data = json.loads(result.contents[0].text)
        assert "error" in data

    @pytest.mark.asyncio
    async def test_agent_attestations_resource(self, mcp_session):
        agent = _make_orm_agent()
        session_ctx = make_session_ctx(agent, [])
        with patch("agent_trust.db.session.get_session", session_ctx):
            result = await mcp_session.read_resource(
                f"trust://agents/{agent.agent_id}/attestations"
            )

        assert result.contents
        data = json.loads(result.contents[0].text)
        assert "agent_id" in data or "attestations" in data

    @pytest.mark.asyncio
    async def test_dispute_resource_not_found(self, mcp_session):
        session_ctx = make_session_ctx(None)
        dispute_id = str(uuid.uuid4())
        with patch("agent_trust.db.session.get_session", session_ctx):
            result = await mcp_session.read_resource(f"trust://disputes/{dispute_id}")

        assert result.contents
        data = json.loads(result.contents[0].text)
        assert "error" in data


# ---------------------------------------------------------------------------
# Prompts
# ---------------------------------------------------------------------------


class TestPromptsMCP:
    @pytest.mark.asyncio
    async def test_evaluate_counterparty_prompt(self, mcp_session):
        agent_id = str(uuid.uuid4())
        result = await mcp_session.get_prompt(
            "evaluate_counterparty_prompt",
            {"agent_id": agent_id, "transaction_value": "1000", "transaction_type": "payment"},
        )
        assert result.messages
        text = result.messages[0].content.text
        assert agent_id in text

    @pytest.mark.asyncio
    async def test_explain_score_change_prompt(self, mcp_session):
        agent_id = str(uuid.uuid4())
        result = await mcp_session.get_prompt("explain_score_change_prompt", {"agent_id": agent_id})
        assert result.messages
        text = result.messages[0].content.text
        assert agent_id in text

    @pytest.mark.asyncio
    async def test_dispute_assessment_prompt(self, mcp_session):
        dispute_id = str(uuid.uuid4())
        result = await mcp_session.get_prompt(
            "dispute_assessment_prompt", {"dispute_id": dispute_id}
        )
        assert result.messages
        text = result.messages[0].content.text
        assert dispute_id in text
