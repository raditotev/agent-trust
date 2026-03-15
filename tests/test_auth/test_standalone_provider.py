from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from agent_trust.auth.identity import AuthenticationError
from agent_trust.auth.standalone import StandaloneProvider


@pytest.fixture
def provider():
    return StandaloneProvider(db_session=None)


@pytest.mark.asyncio
async def test_authenticate_requires_public_key(provider):
    with pytest.raises(AuthenticationError, match="requires public_key_hex"):
        await provider.authenticate(access_token="some-token")


@pytest.mark.asyncio
async def test_authenticate_invalid_hex(provider):
    with pytest.raises(AuthenticationError, match="Invalid public_key_hex"):
        await provider.authenticate(public_key_hex="not-valid-hex!!!")


@pytest.mark.asyncio
async def test_authenticate_unknown_key(provider):
    provider._lookup_by_public_key = AsyncMock(return_value=None)
    with pytest.raises(AuthenticationError, match="Unknown public key"):
        await provider.authenticate(public_key_hex="deadbeef")


@pytest.mark.asyncio
async def test_authenticate_known_key():
    import uuid

    mock_agent = MagicMock()
    mock_agent.agent_id = uuid.uuid4()

    provider = StandaloneProvider(db_session=None)
    provider._lookup_by_public_key = AsyncMock(return_value=mock_agent)

    identity = await provider.authenticate(public_key_hex="deadbeef01")

    assert identity.source == "standalone"
    assert identity.trust_level == "standalone"
    assert "trust.read" in identity.scopes
    assert "trust.report" in identity.scopes


@pytest.mark.asyncio
async def test_check_permission_always_false(provider):
    from agent_trust.auth.identity import AgentIdentity

    identity = AgentIdentity(agent_id="test", source="standalone", scopes=["trust.read"])
    result = await provider.check_permission(identity, "execute", "/trust/disputes/resolve")
    assert result is False
