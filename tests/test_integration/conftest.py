from __future__ import annotations

import asyncio
import uuid
from contextlib import asynccontextmanager
from unittest.mock import AsyncMock, MagicMock

import pytest_asyncio

from agent_trust.auth.identity import AgentIdentity
from agent_trust.auth.standalone import STANDALONE_SCOPES
from agent_trust.ratelimit import RateLimitResult

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

RATE_LIMIT_ALLOWED = RateLimitResult(allowed=True, limit=60, remaining=59, reset_at=9_999_999_999)

ALL_SCOPES = [
    "trust.read",
    "trust.report",
    "trust.dispute.file",
    "trust.dispute.resolve",
    "trust.attest.issue",
    "trust.admin",
]

# ---------------------------------------------------------------------------
# Identity helpers
# ---------------------------------------------------------------------------


def make_identity(
    agent_id: str | None = None,
    scopes: list[str] | None = None,
    source: str = "agentauth",
    trust_level: str = "delegated",
) -> AgentIdentity:
    """Build an AgentIdentity for use in mock auth patches."""
    return AgentIdentity(
        agent_id=str(agent_id or uuid.uuid4()),
        source=source,
        scopes=scopes or ["trust.read", "trust.report"],
        trust_level=trust_level,
    )


def make_root_identity(agent_id: str | None = None) -> AgentIdentity:
    return make_identity(agent_id=agent_id, scopes=ALL_SCOPES, trust_level="root")


def make_standalone_identity(agent_id: str | None = None) -> AgentIdentity:
    return make_identity(
        agent_id=agent_id, scopes=STANDALONE_SCOPES, source="standalone", trust_level="standalone"
    )


# ---------------------------------------------------------------------------
# Mock Redis factory
# ---------------------------------------------------------------------------


def make_mock_redis() -> AsyncMock:
    """Return a mock Redis client that simulates cache misses and rate-limit operations."""
    redis = AsyncMock()
    redis.get = AsyncMock(return_value=None)
    redis.setex = AsyncMock()
    redis.delete = AsyncMock()
    redis.ping = AsyncMock(return_value=True)
    # Rate-limit sorted-set operations
    redis.zrangebyscore = AsyncMock(return_value=[])
    redis.zadd = AsyncMock()
    redis.zremrangebyscore = AsyncMock()
    redis.zcard = AsyncMock(return_value=0)
    return redis


# ---------------------------------------------------------------------------
# Mock DB session factory
# ---------------------------------------------------------------------------


def make_session_ctx(*execute_results):
    """Return an async-context-manager factory whose session.execute() cycles
    through *execute_results* in order.

    Each element in *execute_results* can be:
    - None            → scalar_one_or_none returns None, scalars().all() returns []
    - an ORM instance → scalar_one_or_none returns it, scalars().all() returns [it]
    - a list          → scalar_one_or_none returns first item (or None), scalars().all() = list
    - a list of tuples→ .all() returns list (for join queries returning rows)
    """
    results = list(execute_results)

    @asynccontextmanager
    async def _ctx():
        session = AsyncMock()
        iterator = iter(results)

        def _execute_side_effect(*args, **kwargs):
            mock_result = MagicMock()
            try:
                val = next(iterator)
            except StopIteration:
                val = None

            if isinstance(val, list):
                if val and isinstance(val[0], tuple):
                    # Join query result — expose via .all() and .scalars().all()
                    mock_result.all.return_value = val
                    mock_result.scalars.return_value.all.return_value = [r[0] for r in val]
                    mock_result.scalar_one_or_none.return_value = val[0][0] if val else None
                    mock_result.scalar.return_value = val[0][0] if val else None
                else:
                    mock_result.all.return_value = val
                    mock_result.scalars.return_value.all.return_value = val
                    mock_result.scalar_one_or_none.return_value = val[0] if val else None
                    mock_result.scalar.return_value = val[0] if val else None
            else:
                mock_result.scalar_one_or_none.return_value = val
                mock_result.scalar.return_value = val
                mock_result.scalars.return_value.all.return_value = [val] if val is not None else []
                mock_result.all.return_value = [(val, None)] if val is not None else []

            return mock_result

        session.execute = AsyncMock(side_effect=_execute_side_effect)
        session.add = MagicMock()
        session.flush = AsyncMock()
        yield session

    return _ctx


# ---------------------------------------------------------------------------
# In-process MCP session fixture
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture
async def mcp_session():
    """In-process MCP ClientSession connected to the AgentTrust FastMCP server.

    Runs the server context manager in a background asyncio task so that
    setup and teardown always happen in the same task — required by anyio
    cancel scopes (pytest-asyncio 1.x runs fixture finalizers in a different
    task which would otherwise raise RuntimeError).
    """
    from mcp.shared.memory import create_connected_server_and_client_session

    from agent_trust.server import mcp as agent_trust_mcp

    ready: asyncio.Event = asyncio.Event()
    stop: asyncio.Event = asyncio.Event()
    session_holder: list = []
    error_holder: list = []

    async def _host() -> None:
        try:
            async with create_connected_server_and_client_session(agent_trust_mcp) as session:
                session_holder.append(session)
                ready.set()
                await stop.wait()
        except Exception as exc:  # pragma: no cover
            error_holder.append(exc)
            ready.set()

    task = asyncio.create_task(_host())
    await ready.wait()

    if error_holder:  # pragma: no cover
        raise error_holder[0]

    try:
        yield session_holder[0]
    finally:
        stop.set()
        await task
