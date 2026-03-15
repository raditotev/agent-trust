from __future__ import annotations

import json
import time
from unittest.mock import AsyncMock

import pytest

from agent_trust.auth.cache import cached_introspect


@pytest.mark.asyncio
async def test_cache_miss_calls_introspect():
    redis_mock = AsyncMock()
    redis_mock.get = AsyncMock(return_value=None)
    redis_mock.setex = AsyncMock()

    introspect_fn = AsyncMock(
        return_value={
            "active": True,
            "sub": "agent-123",
            "exp": int(time.time()) + 3600,
        }
    )

    result = await cached_introspect("test-token", introspect_fn, redis_mock)

    introspect_fn.assert_called_once_with("test-token")
    redis_mock.setex.assert_called_once()
    assert result["active"] is True


@pytest.mark.asyncio
async def test_cache_hit_skips_introspect():
    cached_data = {"active": True, "sub": "agent-123", "exp": int(time.time()) + 3600}
    redis_mock = AsyncMock()
    redis_mock.get = AsyncMock(return_value=json.dumps(cached_data))

    introspect_fn = AsyncMock()

    result = await cached_introspect("test-token", introspect_fn, redis_mock)

    introspect_fn.assert_not_called()
    assert result["sub"] == "agent-123"


@pytest.mark.asyncio
async def test_inactive_token_not_cached():
    redis_mock = AsyncMock()
    redis_mock.get = AsyncMock(return_value=None)
    redis_mock.setex = AsyncMock()

    introspect_fn = AsyncMock(return_value={"active": False})

    await cached_introspect("invalid-token", introspect_fn, redis_mock)

    redis_mock.setex.assert_not_called()


@pytest.mark.asyncio
async def test_ttl_capped_at_300():
    redis_mock = AsyncMock()
    redis_mock.get = AsyncMock(return_value=None)
    redis_mock.setex = AsyncMock()

    # Token expires in 1 hour — TTL should be capped at 300s
    introspect_fn = AsyncMock(
        return_value={
            "active": True,
            "sub": "agent-123",
            "exp": int(time.time()) + 7200,
        }
    )

    await cached_introspect("test-token", introspect_fn, redis_mock)

    call_args = redis_mock.setex.call_args
    ttl = call_args[0][1]
    assert ttl <= 300
