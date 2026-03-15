from __future__ import annotations

import json
import uuid
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from agent_trust.auth.identity import AgentIdentity, AuthenticationError
from agent_trust.tools.scoring import check_trust, compare_agents, get_score_breakdown

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_AGENT_A = str(uuid.uuid4())
_AGENT_B = str(uuid.uuid4())


def _make_identity(agent_id: str = _AGENT_A, scopes: list[str] | None = None) -> AgentIdentity:
    return AgentIdentity(
        agent_id=agent_id,
        source="agentauth",
        scopes=scopes if scopes is not None else ["trust.read"],
        trust_level="delegated",
    )


def _make_agent(agent_id: str) -> MagicMock:
    agent = MagicMock()
    agent.agent_id = uuid.UUID(agent_id)
    agent.display_name = "Test Agent"
    return agent


def _make_trust_score(
    agent_id: str,
    score_type: str = "overall",
    score: float = 0.75,
    confidence: float = 0.8,
    interaction_count: int = 10,
) -> MagicMock:
    ts = MagicMock()
    ts.agent_id = uuid.UUID(agent_id)
    ts.score_type = score_type
    ts.score = score
    ts.confidence = confidence
    ts.interaction_count = interaction_count
    ts.factor_breakdown = {
        "bayesian_raw": 0.76,
        "dispute_penalty": 1.0,
        "interactions_weighted": interaction_count,
        "lost_disputes": 0,
        "alpha": 12.0,
        "beta": 4.0,
    }
    ts.computed_at = datetime.now(UTC)
    return ts


@asynccontextmanager
async def _fake_session_ctx(session_mock):
    yield session_mock


def _session_factory(session_mock):
    """Return a side_effect callable that yields a fresh ctx manager each call."""
    def _factory():
        return _fake_session_ctx(session_mock)
    return _factory


def _make_session_for_agent(agent: MagicMock | None, trust_score: MagicMock | None = None):
    """Mock DB session: call 0 → agent lookup, call 1 → trust score lookup."""
    session = MagicMock()
    call_count = [0]

    async def execute(query):
        result = MagicMock()
        n = call_count[0]
        call_count[0] += 1
        if n == 0:
            result.scalar_one_or_none = MagicMock(return_value=agent)
        else:
            result.scalar_one_or_none = MagicMock(return_value=trust_score)
        return result

    session.execute = execute
    session.add = MagicMock()
    session.flush = AsyncMock()
    return session


def _make_redis_mock(cached: dict | None = None):
    redis_mock = AsyncMock()
    redis_mock.get = AsyncMock(return_value=json.dumps(cached) if cached else None)
    redis_mock.setex = AsyncMock()
    return redis_mock


# ---------------------------------------------------------------------------
# Tests: check_trust
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_check_trust_basic():
    """Agent exists → returns score, confidence, interaction_count (unauthenticated)."""
    agent = _make_agent(_AGENT_A)
    trust_score = _make_trust_score(_AGENT_A)
    session = _make_session_for_agent(agent, trust_score)
    redis_mock = _make_redis_mock()

    with (
        patch("agent_trust.tools.scoring.get_session", side_effect=_session_factory(session)),
        patch("agent_trust.tools.scoring.get_redis", new=AsyncMock(return_value=redis_mock)),
    ):
        result = await check_trust(agent_id=_AGENT_A)

    assert "error" not in result
    assert result["agent_id"] == _AGENT_A
    assert result["score_type"] == "overall"
    assert "score" in result
    assert "confidence" in result
    assert "interaction_count" in result
    assert "factor_breakdown" not in result  # unauthenticated


@pytest.mark.asyncio
async def test_check_trust_with_auth_adds_breakdown():
    """Authenticated with trust.read → factor_breakdown included in response."""
    agent = _make_agent(_AGENT_A)
    trust_score = _make_trust_score(_AGENT_A)
    session = _make_session_for_agent(agent, trust_score)
    identity = _make_identity(_AGENT_A, scopes=["trust.read"])
    redis_mock = _make_redis_mock()

    mock_provider = MagicMock()
    mock_provider.authenticate = AsyncMock(return_value=identity)

    with (
        patch("agent_trust.tools.scoring.get_session", side_effect=_session_factory(session)),
        patch("agent_trust.tools.scoring.get_redis", new=AsyncMock(return_value=redis_mock)),
        patch("agent_trust.tools.scoring.AgentAuthProvider", return_value=mock_provider),
    ):
        result = await check_trust(agent_id=_AGENT_A, access_token="tok")

    assert "error" not in result
    assert "factor_breakdown" in result
    assert result["authenticated_as"] == _AGENT_A


@pytest.mark.asyncio
async def test_check_trust_agent_not_found():
    """Unknown agent_id → returns error dict."""
    session = MagicMock()

    async def execute(query):
        r = MagicMock()
        r.scalar_one_or_none = MagicMock(return_value=None)
        return r

    session.execute = execute

    with patch("agent_trust.tools.scoring.get_session", side_effect=_session_factory(session)):
        result = await check_trust(agent_id=str(uuid.uuid4()))

    assert "error" in result
    assert "not found" in result["error"]


@pytest.mark.asyncio
async def test_check_trust_invalid_score_type():
    """Bad score_type → returns error without hitting DB."""
    result = await check_trust(agent_id=_AGENT_A, score_type="bogus_type")
    assert "error" in result
    assert "Invalid score_type" in result["error"]


@pytest.mark.asyncio
async def test_check_trust_invalid_uuid():
    """Malformed UUID → returns error."""
    result = await check_trust(agent_id="not-a-uuid")
    assert "error" in result
    assert "Invalid agent_id UUID" in result["error"]


@pytest.mark.asyncio
async def test_check_trust_uses_cache():
    """Cache hit → returns cached score without a second DB roundtrip."""
    cached_data = {
        "agent_id": _AGENT_A,
        "score_type": "overall",
        "score": 0.85,
        "confidence": 0.9,
        "interaction_count": 20,
        "factor_breakdown": {},
        "computed_at": datetime.now(UTC).isoformat(),
    }
    agent = _make_agent(_AGENT_A)
    # Only agent-existence check happens; score comes from cache
    session = _make_session_for_agent(agent)
    redis_mock = _make_redis_mock(cached=cached_data)

    with (
        patch("agent_trust.tools.scoring.get_session", side_effect=_session_factory(session)),
        patch("agent_trust.tools.scoring.get_redis", new=AsyncMock(return_value=redis_mock)),
    ):
        result = await check_trust(agent_id=_AGENT_A)

    assert result["score"] == 0.85
    assert result["interaction_count"] == 20
    # Cache was hit → setex must NOT have been called
    redis_mock.setex.assert_not_called()


# ---------------------------------------------------------------------------
# Tests: get_score_breakdown
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_get_score_breakdown_requires_auth():
    """No valid token → error returned."""
    mock_provider = MagicMock()
    mock_provider.authenticate = AsyncMock(side_effect=AuthenticationError("invalid token"))
    redis_mock = _make_redis_mock()

    with (
        patch("agent_trust.tools.scoring.get_redis", new=AsyncMock(return_value=redis_mock)),
        patch("agent_trust.tools.scoring.AgentAuthProvider", return_value=mock_provider),
    ):
        result = await get_score_breakdown(agent_id=_AGENT_A, access_token="bad-token")

    assert "error" in result


@pytest.mark.asyncio
async def test_get_score_breakdown_requires_trust_read_scope():
    """Token valid but missing trust.read → error returned."""
    identity = _make_identity(_AGENT_A, scopes=["trust.report"])
    mock_provider = MagicMock()
    mock_provider.authenticate = AsyncMock(return_value=identity)
    redis_mock = _make_redis_mock()

    with (
        patch("agent_trust.tools.scoring.get_redis", new=AsyncMock(return_value=redis_mock)),
        patch("agent_trust.tools.scoring.AgentAuthProvider", return_value=mock_provider),
    ):
        result = await get_score_breakdown(agent_id=_AGENT_A, access_token="tok")

    assert "error" in result
    assert "trust.read" in result["error"]


@pytest.mark.asyncio
async def test_get_score_breakdown_returns_all_types():
    """Authenticated with trust.read → all 4 score dimensions returned."""
    identity = _make_identity(_AGENT_A, scopes=["trust.read"])
    agent = _make_agent(_AGENT_A)
    mock_provider = MagicMock()
    mock_provider.authenticate = AsyncMock(return_value=identity)

    score_types = ["overall", "reliability", "responsiveness", "honesty"]
    # Session: 1 agent check + 4 score lookups = 5 execute() calls
    session = MagicMock()
    call_count = [0]

    async def execute(query):
        r = MagicMock()
        n = call_count[0]
        call_count[0] += 1
        if n == 0:
            r.scalar_one_or_none = MagicMock(return_value=agent)
        else:
            st = score_types[(n - 1) % len(score_types)]
            r.scalar_one_or_none = MagicMock(
                return_value=_make_trust_score(_AGENT_A, score_type=st)
            )
        return r

    session.execute = execute
    session.add = MagicMock()
    session.flush = AsyncMock()
    redis_mock = _make_redis_mock()

    with (
        patch("agent_trust.tools.scoring.get_session", side_effect=_session_factory(session)),
        patch("agent_trust.tools.scoring.get_redis", new=AsyncMock(return_value=redis_mock)),
        patch("agent_trust.tools.scoring.AgentAuthProvider", return_value=mock_provider),
    ):
        result = await get_score_breakdown(agent_id=_AGENT_A, access_token="tok")

    assert "error" not in result
    assert result["agent_id"] == _AGENT_A
    assert "scores" in result
    for st in ("overall", "reliability", "responsiveness", "honesty"):
        assert st in result["scores"], f"Missing score type: {st}"
    assert result["computed_by"] == _AGENT_A


# ---------------------------------------------------------------------------
# Tests: compare_agents
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_compare_agents_ranked():
    """Two agents → higher score agent ranks first."""
    agent_a = _make_agent(_AGENT_A)
    agent_b = _make_agent(_AGENT_B)
    ts_a = _make_trust_score(_AGENT_A, score=0.9)
    ts_b = _make_trust_score(_AGENT_B, score=0.4)

    # compare_agents calls get_session() twice per agent:
    #   call 0: agent_a existence check
    #   call 1: agent_a score lookup (in _get_or_compute_score)
    #   call 2: agent_b existence check
    #   call 3: agent_b score lookup
    session = MagicMock()
    call_count = [0]
    responses = [agent_a, ts_a, agent_b, ts_b]

    async def execute(query):
        r = MagicMock()
        n = call_count[0]
        call_count[0] += 1
        r.scalar_one_or_none = MagicMock(return_value=responses[n])
        return r

    session.execute = execute
    session.add = MagicMock()
    session.flush = AsyncMock()
    redis_mock = _make_redis_mock()

    with (
        patch("agent_trust.tools.scoring.get_session", side_effect=_session_factory(session)),
        patch("agent_trust.tools.scoring.get_redis", new=AsyncMock(return_value=redis_mock)),
    ):
        result = await compare_agents(agent_ids=[_AGENT_A, _AGENT_B])

    assert "error" not in result
    assert result["count"] == 2
    agents = result["agents"]
    assert agents[0]["agent_id"] == _AGENT_A
    assert agents[0]["rank"] == 1
    assert agents[1]["rank"] == 2


@pytest.mark.asyncio
async def test_compare_agents_max_limit():
    """11 agents → error about max limit."""
    result = await compare_agents(agent_ids=[str(uuid.uuid4()) for _ in range(11)])
    assert "error" in result
    assert "Maximum" in result["error"]


@pytest.mark.asyncio
async def test_compare_agents_empty_list():
    """Empty list → error."""
    result = await compare_agents(agent_ids=[])
    assert "error" in result
