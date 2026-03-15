from __future__ import annotations

import structlog

log = structlog.get_logger()


async def refresh_all_scores(ctx: dict) -> dict:
    """arq periodic task: Refresh decayed scores for all active agents.

    Runs periodically (e.g., nightly) to apply time decay to all agents.
    Agents with no recent interactions will see their scores drift toward 0.5.
    """
    from sqlalchemy import select

    from agent_trust.config import settings
    from agent_trust.db.session import get_session
    from agent_trust.engine.score_engine import SCORE_TYPES, ScoreComputation, upsert_trust_score
    from agent_trust.models import Agent

    log.info("decay_refresh_start")

    engine = ScoreComputation(
        half_life_days=settings.score_half_life_days,
        dispute_penalty_per=settings.dispute_penalty,
    )

    refreshed = 0
    errors = 0

    async with get_session() as session:
        result = await session.execute(select(Agent).where(Agent.status == "active"))
        agents = result.scalars().all()

        for agent in agents:
            for score_type in SCORE_TYPES:
                try:
                    trust_score = await engine.compute(agent.agent_id, score_type, session)
                    await upsert_trust_score(trust_score, session)
                    refreshed += 1
                except Exception as e:
                    log.error(
                        "decay_refresh_failed",
                        agent_id=str(agent.agent_id),
                        score_type=score_type,
                        error=str(e),
                    )
                    errors += 1

    try:
        from agent_trust.db.redis import get_redis

        redis = await get_redis()
        cursor = 0
        deleted = 0
        while True:
            cursor, keys = await redis.scan(cursor, match="score:*", count=100)
            if keys:
                await redis.delete(*keys)
                deleted += len(keys)
            if cursor == 0:
                break
        log.info("score_cache_flushed", deleted_keys=deleted)
    except Exception as e:
        log.warning("cache_flush_failed", error=str(e))

    log.info("decay_refresh_done", refreshed=refreshed, errors=errors)
    return {"refreshed": refreshed, "errors": errors}
