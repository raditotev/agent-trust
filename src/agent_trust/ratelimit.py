from __future__ import annotations

import time
from collections import defaultdict
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

# In-memory fallback counters for transient Redis failures.
# Structure: {key: list_of_timestamps}. Not shared across processes,
# so this is a best-effort grace window, not a strict enforcer.
_fallback_counters: dict[str, list[float]] = defaultdict(list)
_FALLBACK_GRACE_MULTIPLIER = 0.5  # allow 50% of normal rate during fallback


@dataclass
class RateLimitResult:
    allowed: bool
    limit: int
    remaining: int
    reset_at: int  # unix timestamp when window resets
    retry_after: int | None = None  # seconds to wait if not allowed


def _compute_limit(agent_id: str | None, trust_level: str | None) -> int:
    if agent_id is None:
        return settings.rate_limit_unauthenticated
    multiplier = TRUST_LEVEL_MULTIPLIERS.get(trust_level or "standalone", 1.0)
    return int(settings.rate_limit_base * multiplier)


def _fallback_check(key: str, limit: int, window: int = 60) -> RateLimitResult:
    """In-memory sliding window for transient Redis failures.

    Allows a reduced rate (50% of normal) to avoid full outage.
    """
    now = time.time()
    grace_limit = max(1, int(limit * _FALLBACK_GRACE_MULTIPLIER))

    # Prune old entries
    _fallback_counters[key] = [t for t in _fallback_counters[key] if now - t < window]
    current = len(_fallback_counters[key])

    if current >= grace_limit:
        return RateLimitResult(
            allowed=False,
            limit=grace_limit,
            remaining=0,
            reset_at=int(now) + window,
            retry_after=window,
        )

    _fallback_counters[key].append(now)
    return RateLimitResult(
        allowed=True,
        limit=grace_limit,
        remaining=max(0, grace_limit - current - 1),
        reset_at=int(now) + window,
    )


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
    Falls back to an in-memory grace window at reduced rate if Redis is
    transiently unavailable, to avoid full service outage.
    """
    limit = _compute_limit(agent_id, trust_level)

    try:
        redis = await get_redis()
    except Exception as e:
        log.warning("rate_limit_redis_unavailable", error=str(e), action="fallback_in_memory")
        key = f"rl:{agent_id or 'anon'}:{tool_name}"
        return _fallback_check(key, limit)

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
        log.warning("rate_limit_check_failed", error=str(e), action="fallback_in_memory")
        return _fallback_check(key, limit)

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
