from __future__ import annotations

import time
from dataclasses import dataclass

import structlog

from agent_trust.config import settings
from agent_trust.db.redis import get_redis

log = structlog.get_logger()

TRUST_LEVEL_MULTIPLIERS = {
    "root": settings.rate_limit_root_multiplier,
    "delegated": settings.rate_limit_delegated_multiplier,
    "standalone": settings.rate_limit_standalone_multiplier,
    "ephemeral": settings.rate_limit_ephemeral_multiplier,
}


@dataclass
class RateLimitResult:
    allowed: bool
    limit: int
    remaining: int
    reset_at: int  # unix timestamp when window resets
    retry_after: int | None = None  # seconds to wait if not allowed


async def check_rate_limit(
    agent_id: str | None,
    tool_name: str,
    trust_level: str | None = None,
) -> RateLimitResult:
    """Check and increment the sliding-window rate limit for an agent+tool combo.

    Uses a Redis sorted set (ZSET) per agent+tool key.
    Members are request timestamps; expired members are pruned on each check.
    Window is 60 seconds.

    Returns RateLimitResult with allowed=False and retry_after if over limit.
    Fails closed (allowed=False) if Redis is unavailable for security.
    """
    try:
        redis = await get_redis()
    except Exception as e:
        log.warning("rate_limit_redis_unavailable", error=str(e), action="fail_closed")
        if agent_id is None:
            limit = settings.rate_limit_unauthenticated
        else:
            multiplier = TRUST_LEVEL_MULTIPLIERS.get(trust_level or "standalone", 1.0)
            limit = int(settings.rate_limit_base * multiplier)
        return RateLimitResult(
            allowed=False,
            limit=limit,
            remaining=0,
            reset_at=int(time.time()) + 10,
            retry_after=10,
        )

    window_seconds = 60
    now_ms = int(time.time() * 1000)
    window_start_ms = now_ms - window_seconds * 1000

    if agent_id is None:
        limit = settings.rate_limit_unauthenticated
        key = f"rl:anon:{tool_name}"
    else:
        multiplier = TRUST_LEVEL_MULTIPLIERS.get(trust_level or "standalone", 1.0)
        limit = int(settings.rate_limit_base * multiplier)
        key = f"rl:{agent_id}:{tool_name}"

    try:
        # Sliding window: count requests in last 60s
        pipe = redis.pipeline()
        pipe.zremrangebyscore(key, 0, window_start_ms)  # prune old
        pipe.zcard(key)  # count current
        pipe.zadd(key, {str(now_ms): now_ms})  # add this request
        pipe.expire(key, window_seconds * 2)  # TTL safety
        results = await pipe.execute()
    except Exception as e:
        log.warning("rate_limit_check_failed", error=str(e), action="fail_closed")
        return RateLimitResult(
            allowed=False,
            limit=limit,
            remaining=0,
            reset_at=int(time.time()) + 10,
            retry_after=10,
        )

    current_count = results[1]  # count BEFORE adding this request

    if current_count >= limit:
        oldest = await redis.zrange(key, 0, 0, withscores=True)
        if oldest:
            oldest_ms = int(oldest[0][1])
            retry_after = max(1, window_seconds - int((now_ms - oldest_ms) / 1000))
        else:
            retry_after = window_seconds

        log.warning(
            "rate_limit_exceeded",
            agent_id=agent_id,
            tool=tool_name,
            count=current_count,
            limit=limit,
        )
        return RateLimitResult(
            allowed=False,
            limit=limit,
            remaining=0,
            reset_at=int(time.time()) + retry_after,
            retry_after=retry_after,
        )

    remaining = max(0, limit - current_count - 1)
    return RateLimitResult(
        allowed=True,
        limit=limit,
        remaining=remaining,
        reset_at=int(time.time()) + window_seconds,
    )


def rate_limited(tool_name: str) -> None:
    """Document the pattern for applying rate limiting to MCP tools.

    Usage in a tool:
        result = await check_rate_limit(
            agent_id=identity.agent_id if identity else None,
            tool_name="tool_name",
            trust_level=identity.trust_level if identity else None,
        )
        if not result.allowed:
            return {"error": "Rate limit exceeded", "retry_after": result.retry_after}

    Note: Direct decorator wrapping is complex with async tools.
    Prefer inline calls to check_rate_limit in each tool.
    """
    pass
