from __future__ import annotations

from unittest.mock import patch

import pytest

from tests.mocks.agentauth import MOCK_DELEGATED_AGENT, MOCK_ROOT_AGENT, make_token_introspection


@pytest.mark.asyncio
async def test_agentauth_introspection_extracts_identity():
    """AgentAuthProvider correctly extracts identity from introspection response."""
    from agent_trust.auth.agentauth import AgentAuthProvider

    provider = AgentAuthProvider(redis_client=None)
    mock_response = make_token_introspection(
        agent_id="test-agent-uuid",
        scopes=["trust.read", "trust.report", "trust.attest.issue"],
        trust_level="root",
    )

    with patch.object(provider, "_introspect_token_raw", return_value=mock_response):
        identity = await provider.authenticate(access_token="mock-token")

    assert identity.agent_id == "test-agent-uuid"
    assert identity.source == "agentauth"
    assert identity.trust_level == "root"
    assert "trust.attest.issue" in identity.scopes


@pytest.mark.asyncio
async def test_root_agent_has_elevated_trust_level():
    """Root agents have trust_level='root' in their identity."""
    from agent_trust.auth.agentauth import AgentAuthProvider

    provider = AgentAuthProvider(redis_client=None)
    with patch.object(provider, "_introspect_token_raw", return_value=MOCK_ROOT_AGENT):
        identity = await provider.authenticate(access_token="root-token")
    assert identity.trust_level == "root"


@pytest.mark.asyncio
async def test_delegated_agent_identity():
    """Delegated agents have trust_level='delegated'."""
    from agent_trust.auth.agentauth import AgentAuthProvider

    provider = AgentAuthProvider(redis_client=None)
    with patch.object(provider, "_introspect_token_raw", return_value=MOCK_DELEGATED_AGENT):
        identity = await provider.authenticate(access_token="delegated-token")
    assert identity.trust_level == "delegated"
    assert "trust.dispute.file" in identity.scopes
