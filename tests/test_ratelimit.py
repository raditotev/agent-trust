from __future__ import annotations

import time
from unittest.mock import AsyncMock, MagicMock

import pytest

from agent_trust.ratelimit import RateLimitResult, check_rate_limit


def make_redis_mock(current_count: int = 0) -> AsyncMock:
    """Mock Redis pipeline that simulates a given current_count."""
    redis = AsyncMock()
    pipe = AsyncMock()
    pipe.zremrangebyscore = MagicMock()
    pipe.zcard = MagicMock()
    pipe.zadd = MagicMock()
    pipe.expire = MagicMock()
    pipe.execute = AsyncMock(return_value=[None, current_count, None, None])
    redis.pipeline = MagicMock(return_value=pipe)
    redis.zrange = AsyncMock(return_value=[])
    return redis


@pytest.mark.asyncio
async def test_rate_limit_allowed() -> None:
    redis_mock = make_redis_mock(current_count=5)

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr("agent_trust.ratelimit.get_redis", AsyncMock(return_value=redis_mock))
        result = await check_rate_limit("agent-123", "check_trust", "delegated")

    assert result.allowed is True
    assert result.limit == 120  # base 60 * delegated 2.0
    assert result.remaining >= 0


@pytest.mark.asyncio
async def test_rate_limit_exceeded() -> None:
    redis_mock = make_redis_mock(current_count=120)  # at limit for delegated

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr("agent_trust.ratelimit.get_redis", AsyncMock(return_value=redis_mock))
        result = await check_rate_limit("agent-123", "check_trust", "delegated")

    assert result.allowed is False
    assert result.retry_after is not None
    assert result.retry_after > 0


@pytest.mark.asyncio
async def test_rate_limit_unauthenticated() -> None:
    redis_mock = make_redis_mock(current_count=5)

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr("agent_trust.ratelimit.get_redis", AsyncMock(return_value=redis_mock))
        result = await check_rate_limit(None, "check_trust", None)

    assert result.limit == 10  # unauthenticated limit
    assert result.allowed is True


@pytest.mark.asyncio
async def test_rate_limit_root_higher_limit() -> None:
    redis_mock = make_redis_mock(current_count=0)

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr("agent_trust.ratelimit.get_redis", AsyncMock(return_value=redis_mock))
        result = await check_rate_limit("root-agent", "check_trust", "root")

    assert result.limit == 300  # base 60 * root 5.0


@pytest.mark.asyncio
async def test_rate_limit_standalone_limit() -> None:
    redis_mock = make_redis_mock(current_count=0)

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr("agent_trust.ratelimit.get_redis", AsyncMock(return_value=redis_mock))
        result = await check_rate_limit("agent-xyz", "check_trust", "standalone")

    assert result.limit == 60  # base 60 * standalone 1.0


@pytest.mark.asyncio
async def test_rate_limit_ephemeral_limit() -> None:
    redis_mock = make_redis_mock(current_count=0)

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr("agent_trust.ratelimit.get_redis", AsyncMock(return_value=redis_mock))
        result = await check_rate_limit("agent-xyz", "check_trust", "ephemeral")

    assert result.limit == 30  # base 60 * ephemeral 0.5


@pytest.mark.asyncio
async def test_rate_limit_remaining_decrements() -> None:
    redis_mock = make_redis_mock(current_count=10)

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr("agent_trust.ratelimit.get_redis", AsyncMock(return_value=redis_mock))
        result = await check_rate_limit("agent-123", "check_trust", "standalone")

    assert result.allowed is True
    assert result.remaining == 49  # 60 - 10 - 1


@pytest.mark.asyncio
async def test_rate_limit_retry_after_uses_oldest_entry() -> None:
    redis_mock = make_redis_mock(current_count=60)
    # Simulate oldest entry 30 seconds ago
    now_ms = int(time.time() * 1000)
    oldest_ms = now_ms - 30_000
    redis_mock.zrange = AsyncMock(return_value=[("ts", oldest_ms)])

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr("agent_trust.ratelimit.get_redis", AsyncMock(return_value=redis_mock))
        result = await check_rate_limit("agent-123", "check_trust", "standalone")

    assert result.allowed is False
    # retry_after should be ~30s (60 - 30)
    assert 25 <= result.retry_after <= 35


@pytest.mark.asyncio
async def test_rate_limit_anon_key_uses_tool_name() -> None:
    redis_mock = make_redis_mock(current_count=0)
    pipe = redis_mock.pipeline.return_value

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr("agent_trust.ratelimit.get_redis", AsyncMock(return_value=redis_mock))
        await check_rate_limit(None, "report_interaction", None)

    # Key should be rl:anon:report_interaction
    zadd_call = pipe.zadd.call_args
    assert zadd_call is not None
    key_arg = zadd_call[0][0]
    assert key_arg == "rl:anon:report_interaction"


def test_rate_limit_result_fields() -> None:
    result = RateLimitResult(
        allowed=True, limit=60, remaining=55, reset_at=int(time.time()) + 60
    )
    assert result.allowed is True
    assert result.retry_after is None
    assert result.remaining == 55
    assert result.limit == 60


def test_rate_limit_result_denied_fields() -> None:
    result = RateLimitResult(
        allowed=False, limit=60, remaining=0, reset_at=int(time.time()) + 30, retry_after=30
    )
    assert result.allowed is False
    assert result.retry_after == 30
    assert result.remaining == 0
