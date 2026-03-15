from __future__ import annotations

from unittest.mock import patch

import pytest

from agent_trust.auth.agentauth import AgentAuthProvider
from agent_trust.auth.identity import AuthenticationError
from tests.mocks.agentauth import make_token_introspection


@pytest.fixture
def provider():
    return AgentAuthProvider(redis_client=None)


@pytest.mark.asyncio
async def test_authenticate_valid_token(provider):
    mock_introspection = make_token_introspection(
        agent_id="test-agent-123",
        scopes=["trust.read", "trust.report"],
        trust_level="delegated",
    )
    with patch.object(provider, "_introspect_token_raw", return_value=mock_introspection):
        identity = await provider.authenticate(access_token="mock-token")

    assert identity.agent_id == "test-agent-123"
    assert identity.source == "agentauth"
    assert "trust.read" in identity.scopes
    assert identity.trust_level == "delegated"


@pytest.mark.asyncio
async def test_authenticate_expired_token(provider):
    mock_inactive = {"active": False}
    with patch.object(provider, "_introspect_token_raw", return_value=mock_inactive):
        with pytest.raises(AuthenticationError, match="Invalid or expired"):
            await provider.authenticate(access_token="expired-token")


@pytest.mark.asyncio
async def test_authenticate_requires_access_token(provider):
    with pytest.raises(AuthenticationError, match="requires access_token"):
        await provider.authenticate(public_key_hex="abc123")


@pytest.mark.asyncio
async def test_scopes_extracted_from_list(provider):
    mock_introspection = make_token_introspection(
        scopes=["trust.read", "trust.report", "trust.admin"],
        trust_level="root",
    )
    with patch.object(provider, "_introspect_token_raw", return_value=mock_introspection):
        identity = await provider.authenticate(access_token="mock-token")

    assert "trust.admin" in identity.scopes
    assert identity.trust_level == "root"


@pytest.mark.asyncio
async def test_scopes_extracted_from_string(provider):
    mock_introspection = {
        "active": True,
        "sub": "agent-xyz",
        "scope": "trust.read trust.report",
        "trust_level": "ephemeral",
        "exp": 9999999999,
    }
    with patch.object(provider, "_introspect_token_raw", return_value=mock_introspection):
        identity = await provider.authenticate(access_token="mock-token")

    assert "trust.read" in identity.scopes
    assert "trust.report" in identity.scopes
