from __future__ import annotations

import uuid
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from agent_trust.auth.identity import AgentIdentity
from agent_trust.ratelimit import RateLimitResult
from agent_trust.tools.disputes import file_dispute, resolve_dispute

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_FILER_ID = str(uuid.uuid4())
_COUNTERPARTY_ID = str(uuid.uuid4())
_RESOLVER_ID = str(uuid.uuid4())
_INTERACTION_ID = str(uuid.uuid4())
_DISPUTE_ID = str(uuid.uuid4())

_RATE_LIMIT_ALLOWED = RateLimitResult(
    allowed=True, limit=60, remaining=59, reset_at=9_999_999_999
)


def _make_identity(
    agent_id: str = _FILER_ID,
    scopes: list[str] | None = None,
) -> AgentIdentity:
    return AgentIdentity(
        agent_id=agent_id,
        source="agentauth",
        scopes=scopes if scopes is not None else ["trust.dispute.file"],
        trust_level="delegated",
    )


def _make_interaction(
    initiator_id: str = _FILER_ID,
    counterparty_id: str = _COUNTERPARTY_ID,
) -> MagicMock:
    ix = MagicMock()
    ix.interaction_id = uuid.UUID(_INTERACTION_ID)
    ix.initiator_id = uuid.UUID(initiator_id)
    ix.counterparty_id = uuid.UUID(counterparty_id)
    return ix


def _make_dispute(status: str = "open") -> MagicMock:
    d = MagicMock()
    d.dispute_id = uuid.UUID(_DISPUTE_ID)
    d.interaction_id = uuid.UUID(_INTERACTION_ID)
    d.filed_by = uuid.UUID(_FILER_ID)
    d.filed_against = uuid.UUID(_COUNTERPARTY_ID)
    d.status = status
    d.resolved_at = datetime.now(UTC)
    return d


@asynccontextmanager
async def _fake_session_ctx(session_mock):
    yield session_mock


def _make_session(*scalar_results):
    """Return a session mock that cycles through scalar_results per execute call.
    
    Returns results via both .scalar() and .scalar_one_or_none() for compatibility.
    """
    session = MagicMock()
    session.add = MagicMock()
    session.flush = AsyncMock()

    call_count = [0]

    async def execute(_query):
        result = MagicMock()
        n = call_count[0]
        call_count[0] += 1
        value = scalar_results[n] if n < len(scalar_results) else None
        result.scalar_one_or_none.return_value = value
        result.scalar.return_value = value
        return result

    session.execute = execute
    return session


# ---------------------------------------------------------------------------
# file_dispute tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_file_dispute_success():
    interaction = _make_interaction()
    # Query sequence: dismissed_count, last_dismissed_at, interaction, existing_dispute
    session = _make_session(0, None, interaction, None)

    provider = MagicMock()
    provider.authenticate = AsyncMock(return_value=_make_identity())

    with (
        patch("agent_trust.tools.disputes._get_agentauth_provider", AsyncMock(return_value=provider)),
        patch("agent_trust.tools.disputes.get_session", return_value=_fake_session_ctx(session)),
        patch("agent_trust.ratelimit.check_rate_limit", AsyncMock(return_value=_RATE_LIMIT_ALLOWED)),
    ):
        result = await file_dispute(
            interaction_id=_INTERACTION_ID,
            reason="Outcome was falsely reported",
            access_token="tok",
        )

    assert "dispute_id" in result
    assert result["status"] == "open"
    assert result["interaction_id"] == _INTERACTION_ID


@pytest.mark.asyncio
async def test_file_dispute_requires_scope():
    provider = MagicMock()
    provider.authenticate = AsyncMock(
        return_value=_make_identity(scopes=["trust.read"])
    )

    with patch("agent_trust.tools.disputes._get_agentauth_provider", AsyncMock(return_value=provider)):
        result = await file_dispute(
            interaction_id=_INTERACTION_ID,
            reason="reason",
            access_token="tok",
        )

    assert "error" in result
    assert "trust.dispute.file" in result["error"]


@pytest.mark.asyncio
async def test_file_dispute_not_a_party():
    other_id = str(uuid.uuid4())
    interaction = _make_interaction()  # initiator=_FILER_ID, counterparty=_COUNTERPARTY_ID
    # Query sequence: dismissed_count, last_dismissed_at, interaction
    session = _make_session(0, None, interaction)

    provider = MagicMock()
    # Authenticate as someone not party to the interaction
    provider.authenticate = AsyncMock(return_value=_make_identity(agent_id=other_id))

    with (
        patch("agent_trust.tools.disputes._get_agentauth_provider", AsyncMock(return_value=provider)),
        patch("agent_trust.tools.disputes.get_session", return_value=_fake_session_ctx(session)),
        patch("agent_trust.ratelimit.check_rate_limit", AsyncMock(return_value=_RATE_LIMIT_ALLOWED)),
    ):
        result = await file_dispute(
            interaction_id=_INTERACTION_ID,
            reason="reason",
            access_token="tok",
        )

    assert "error" in result
    assert "party" in result["error"]


@pytest.mark.asyncio
async def test_file_dispute_interaction_not_found():
    # Query sequence: dismissed_count, last_dismissed_at, interaction (not found)
    session = _make_session(0, None, None)

    provider = MagicMock()
    provider.authenticate = AsyncMock(return_value=_make_identity())

    with (
        patch("agent_trust.tools.disputes._get_agentauth_provider", AsyncMock(return_value=provider)),
        patch("agent_trust.tools.disputes.get_session", return_value=_fake_session_ctx(session)),
        patch("agent_trust.ratelimit.check_rate_limit", AsyncMock(return_value=_RATE_LIMIT_ALLOWED)),
    ):
        result = await file_dispute(
            interaction_id=_INTERACTION_ID,
            reason="reason",
            access_token="tok",
        )

    assert "error" in result
    assert "not found" in result["error"]


@pytest.mark.asyncio
async def test_file_dispute_duplicate_open():
    interaction = _make_interaction()
    existing_dispute = _make_dispute(status="open")
    # Query sequence: dismissed_count, last_dismissed_at, interaction, existing_dispute
    session = _make_session(0, None, interaction, existing_dispute)

    provider = MagicMock()
    provider.authenticate = AsyncMock(return_value=_make_identity())

    with (
        patch("agent_trust.tools.disputes._get_agentauth_provider", AsyncMock(return_value=provider)),
        patch("agent_trust.tools.disputes.get_session", return_value=_fake_session_ctx(session)),
        patch("agent_trust.ratelimit.check_rate_limit", AsyncMock(return_value=_RATE_LIMIT_ALLOWED)),
    ):
        result = await file_dispute(
            interaction_id=_INTERACTION_ID,
            reason="reason",
            access_token="tok",
        )

    assert "error" in result
    assert "already" in result["error"]


# ---------------------------------------------------------------------------
# resolve_dispute tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_resolve_dispute_success():
    dispute = _make_dispute(status="open")
    session = _make_session(dispute)

    resolver_identity = _make_identity(
        agent_id=_RESOLVER_ID,
        scopes=["trust.dispute.resolve"],
    )
    provider = MagicMock()
    provider.authenticate = AsyncMock(return_value=resolver_identity)
    provider.check_permission = AsyncMock(return_value=True)

    with (
        patch("agent_trust.tools.disputes._get_agentauth_provider", AsyncMock(return_value=provider)),
        patch("agent_trust.tools.disputes.get_session", return_value=_fake_session_ctx(session)),
        patch("agent_trust.tools.disputes._enqueue_dispute_recomputation", AsyncMock()),
    ):
        result = await resolve_dispute(
            dispute_id=_DISPUTE_ID,
            resolution="upheld",
            access_token="tok",
            resolution_note="Evidence was clear.",
        )

    assert result["status"] == "resolved"
    assert result["resolution"] == "upheld"
    assert result["resolved_by"] == _RESOLVER_ID


@pytest.mark.asyncio
async def test_resolve_dispute_requires_scope():
    provider = MagicMock()
    provider.authenticate = AsyncMock(
        return_value=_make_identity(scopes=["trust.read"])
    )

    with patch("agent_trust.tools.disputes._get_agentauth_provider", AsyncMock(return_value=provider)):
        result = await resolve_dispute(
            dispute_id=_DISPUTE_ID,
            resolution="upheld",
            access_token="tok",
        )

    assert "error" in result
    assert "trust.dispute.resolve" in result["error"]


@pytest.mark.asyncio
async def test_resolve_dispute_requires_agentauth_permission():
    resolver_identity = _make_identity(
        agent_id=_RESOLVER_ID,
        scopes=["trust.dispute.resolve"],
    )
    provider = MagicMock()
    provider.authenticate = AsyncMock(return_value=resolver_identity)
    provider.check_permission = AsyncMock(return_value=False)  # AgentAuth denies

    with patch("agent_trust.tools.disputes._get_agentauth_provider", AsyncMock(return_value=provider)):
        result = await resolve_dispute(
            dispute_id=_DISPUTE_ID,
            resolution="upheld",
            access_token="tok",
        )

    assert "error" in result
    assert "arbitrator" in result["error"]


@pytest.mark.asyncio
async def test_resolve_dispute_invalid_resolution():
    resolver_identity = _make_identity(
        agent_id=_RESOLVER_ID,
        scopes=["trust.dispute.resolve"],
    )
    provider = MagicMock()
    provider.authenticate = AsyncMock(return_value=resolver_identity)
    provider.check_permission = AsyncMock(return_value=True)

    with patch("agent_trust.tools.disputes._get_agentauth_provider", AsyncMock(return_value=provider)):
        result = await resolve_dispute(
            dispute_id=_DISPUTE_ID,
            resolution="invalid_value",
            access_token="tok",
        )

    assert "error" in result
    assert "Invalid resolution" in result["error"]


@pytest.mark.asyncio
async def test_resolve_dispute_not_found():
    session = _make_session(None)  # dispute not found

    resolver_identity = _make_identity(
        agent_id=_RESOLVER_ID,
        scopes=["trust.dispute.resolve"],
    )
    provider = MagicMock()
    provider.authenticate = AsyncMock(return_value=resolver_identity)
    provider.check_permission = AsyncMock(return_value=True)

    with (
        patch("agent_trust.tools.disputes._get_agentauth_provider", AsyncMock(return_value=provider)),
        patch("agent_trust.tools.disputes.get_session", return_value=_fake_session_ctx(session)),
    ):
        result = await resolve_dispute(
            dispute_id=_DISPUTE_ID,
            resolution="upheld",
            access_token="tok",
        )

    assert "error" in result
    assert "not found" in result["error"]


@pytest.mark.asyncio
async def test_resolve_dispute_already_resolved():
    dispute = _make_dispute(status="resolved")
    session = _make_session(dispute)

    resolver_identity = _make_identity(
        agent_id=_RESOLVER_ID,
        scopes=["trust.dispute.resolve"],
    )
    provider = MagicMock()
    provider.authenticate = AsyncMock(return_value=resolver_identity)
    provider.check_permission = AsyncMock(return_value=True)

    with (
        patch("agent_trust.tools.disputes._get_agentauth_provider", AsyncMock(return_value=provider)),
        patch("agent_trust.tools.disputes.get_session", return_value=_fake_session_ctx(session)),
    ):
        result = await resolve_dispute(
            dispute_id=_DISPUTE_ID,
            resolution="dismissed",
            access_token="tok",
        )

    assert "error" in result
    assert "already" in result["error"]
