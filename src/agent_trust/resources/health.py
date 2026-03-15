from __future__ import annotations

import json
from datetime import UTC, datetime

import structlog

log = structlog.get_logger()


async def get_health() -> str:
    """Service health: DB, Redis, and worker queue status."""
    status: dict = {
        "service": "agent-trust",
        "timestamp": datetime.now(UTC).isoformat(),
        "checks": {},
    }

    try:
        import sqlalchemy

        from agent_trust.db.session import engine

        async with engine.begin() as conn:
            await conn.execute(sqlalchemy.text("SELECT 1"))
        status["checks"]["database"] = {"status": "ok"}
    except Exception as e:
        status["checks"]["database"] = {"status": "error", "detail": str(e)}

    try:
        from agent_trust.db.redis import get_redis

        redis = await get_redis()
        await redis.ping()
        status["checks"]["redis"] = {"status": "ok"}
    except Exception as e:
        status["checks"]["redis"] = {"status": "error", "detail": str(e)}

    try:
        import httpx

        from agent_trust.config import settings

        health_url = settings.agentauth_mcp_url.rstrip("/mcp") + "/health"
        async with httpx.AsyncClient(timeout=5.0) as client:
            resp = await client.get(health_url, follow_redirects=True)
            if resp.status_code < 500:
                status["checks"]["agentauth_mcp"] = {
                    "status": "ok",
                    "http_status": resp.status_code,
                }
            else:
                status["checks"]["agentauth_mcp"] = {
                    "status": "degraded",
                    "http_status": resp.status_code,
                }
    except Exception as e:
        status["checks"]["agentauth_mcp"] = {"status": "unreachable", "detail": str(e)}

    all_ok = all(c["status"] == "ok" for c in status["checks"].values())
    status["overall"] = "healthy" if all_ok else "degraded"

    return json.dumps(status)
