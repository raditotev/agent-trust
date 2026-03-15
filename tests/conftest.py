from __future__ import annotations

import uuid
from collections.abc import AsyncGenerator
from unittest.mock import AsyncMock

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from agent_trust.models import Base
from tests.mocks.agentauth import make_token_introspection

# ---------------------------------------------------------------------------
# Database fixtures (in-memory SQLite via aiosqlite)
# Note: The Agent model uses PostgreSQL-specific ARRAY columns.
# create_all may emit warnings for these but will fall back to generic types.
# Use these fixtures only for models that do not use ARRAY columns.
# ---------------------------------------------------------------------------

TEST_DATABASE_URL = "sqlite+aiosqlite:///:memory:"


@pytest_asyncio.fixture(scope="session")
async def test_engine():
    engine = create_async_engine(TEST_DATABASE_URL, echo=False)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield engine
    await engine.dispose()


@pytest_asyncio.fixture
async def db_session(test_engine) -> AsyncGenerator[AsyncSession]:
    """Provide a clean DB session per test, with rollback isolation."""
    async_session = async_sessionmaker(test_engine, expire_on_commit=False)
    async with async_session() as session:
        async with session.begin():
            yield session
            await session.rollback()


# ---------------------------------------------------------------------------
# Redis mock fixture
# ---------------------------------------------------------------------------


@pytest.fixture
def mock_redis():
    """Mock Redis client for tests — no real Redis needed."""
    redis = AsyncMock()
    redis.get = AsyncMock(return_value=None)  # cache miss by default
    redis.setex = AsyncMock()
    redis.delete = AsyncMock()
    redis.ping = AsyncMock(return_value=True)
    return redis


# ---------------------------------------------------------------------------
# AgentAuth mock fixture
# ---------------------------------------------------------------------------


@pytest.fixture
def mock_agentauth_provider():
    """Mock AgentAuthProvider that returns configurable identities."""
    from agent_trust.auth.identity import AgentIdentity

    provider = AsyncMock()

    default_identity = AgentIdentity(
        agent_id=str(uuid.uuid4()),
        source="agentauth",
        scopes=["trust.read", "trust.report"],
        trust_level="delegated",
    )
    provider.authenticate = AsyncMock(return_value=default_identity)
    provider.check_permission = AsyncMock(return_value=True)
    return provider


@pytest.fixture
def agentauth_introspect_mock():
    """Factory that patches AgentAuthProvider._introspect_token_raw."""
    from unittest.mock import patch

    def factory(response: dict | None = None):
        if response is None:
            response = make_token_introspection()
        return patch(
            "agent_trust.auth.agentauth.AgentAuthProvider._introspect_token_raw",
            new=AsyncMock(return_value=response),
        )

    return factory


# ---------------------------------------------------------------------------
# Agent identity fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def root_agent_id():
    return str(uuid.uuid4())


@pytest.fixture
def delegated_agent_id():
    return str(uuid.uuid4())


@pytest.fixture
def standalone_agent_id():
    return str(uuid.uuid4())
