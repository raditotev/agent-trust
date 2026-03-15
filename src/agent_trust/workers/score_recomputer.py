from __future__ import annotations

import uuid

import structlog

from agent_trust.config import settings
from agent_trust.db.redis import get_redis
from agent_trust.db.session import get_session
from agent_trust.engine.score_engine import SCORE_TYPES, ScoreComputation, upsert_trust_score

log = structlog.get_logger()


async def recompute_score(ctx: dict, agent_id: str) -> dict:
    """arq worker task: Recompute trust scores for an agent.

    Called after an interaction is reported or a dispute is resolved.
    Recomputes all score types and updates the DB + invalidates Redis cache.

    Returns summary of updated scores.
    """
    log.info("recompute_score_start", agent_id=agent_id)

    try:
        agent_uuid = uuid.UUID(agent_id)
    except ValueError:
        log.error("invalid_agent_id", agent_id=agent_id)
        return {"error": f"Invalid UUID: {agent_id}"}

    engine = ScoreComputation(
        half_life_days=settings.score_half_life_days,
        dispute_penalty_per=settings.dispute_penalty,
    )

    updated_scores = {}
    old_overall = None

    async with get_session() as session:
        # Read old overall score before recomputing so we can detect changes
        from sqlalchemy import select

        from agent_trust.models import TrustScore as TrustScoreModel

        try:
            old_result = await session.execute(
                select(TrustScoreModel).where(
                    TrustScoreModel.agent_id == agent_uuid,
                    TrustScoreModel.score_type == "overall",
                )
            )
            old_row = old_result.scalar_one_or_none()
            if old_row:
                old_overall = float(old_row.score)
        except Exception as e:
            log.debug("old_score_fetch_failed", agent_id=agent_id, error=str(e))

        for score_type in SCORE_TYPES:
            try:
                trust_score = await engine.compute(agent_uuid, score_type, session)
                await upsert_trust_score(trust_score, session)
                updated_scores[score_type] = float(trust_score.score)
                log.debug(
                    "score_updated",
                    agent_id=agent_id,
                    score_type=score_type,
                    score=float(trust_score.score),
                )
            except Exception as e:
                log.error(
                    "score_compute_failed",
                    agent_id=agent_id,
                    score_type=score_type,
                    error=str(e),
                )

    try:
        redis = await get_redis()
        for score_type in SCORE_TYPES:
            await redis.delete(f"score:{agent_id}:{score_type}")
        log.debug("score_cache_invalidated", agent_id=agent_id)
    except Exception as e:
        log.warning("cache_invalidation_failed", agent_id=agent_id, error=str(e))

    # Enqueue alert dispatch if the overall score changed
    new_overall = updated_scores.get("overall")
    if old_overall is not None and new_overall is not None:
        try:
            import arq

            redis_settings = arq.connections.RedisSettings.from_dsn(settings.redis_url)
            redis_pool = await arq.create_pool(redis_settings)
            await redis_pool.enqueue_job("dispatch_alerts", agent_id, old_overall, new_overall)
            await redis_pool.aclose()
            log.debug(
                "alert_dispatch_enqueued",
                agent_id=agent_id,
                old_overall=old_overall,
                new_overall=new_overall,
            )
        except Exception as e:
            log.warning("alert_dispatch_enqueue_failed", error=str(e))

    log.info("recompute_score_done", agent_id=agent_id, scores=updated_scores)
    return {"agent_id": agent_id, "updated_scores": updated_scores}
