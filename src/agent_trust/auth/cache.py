from __future__ import annotations

import hashlib
import json
import time

import structlog

log = structlog.get_logger()


async def cached_introspect(
    access_token: str,
    introspect_fn,
    redis_client,
) -> dict:
    """Cache AgentAuth token introspection results in Redis.

    TTL = min(token_expiry_remaining, 300 seconds).
    Uses SHA-256 of the token as the cache key to avoid storing tokens.
    """
    token_hash = hashlib.sha256(access_token.encode()).hexdigest()
    cache_key = f"introspect:{token_hash}"

    cached = await redis_client.get(cache_key)
    if cached:
        log.debug("token_cache_hit", key_prefix=cache_key[:16])
        return json.loads(cached)

    log.debug("token_cache_miss", key_prefix=cache_key[:16])
    result = await introspect_fn(access_token)

    if result.get("active"):
        exp = result.get("exp", 0)
        remaining = max(0, int(exp - time.time())) if exp else 300
        ttl = min(remaining, 300)
        if ttl > 0:
            await redis_client.setex(cache_key, ttl, json.dumps(result))

    return result


async def invalidate_token_cache(access_token: str, redis_client) -> None:
    """Remove a token from the introspection cache (e.g., after revocation)."""
    token_hash = hashlib.sha256(access_token.encode()).hexdigest()
    await redis_client.delete(f"introspect:{token_hash}")
