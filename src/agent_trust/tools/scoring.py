from __future__ import annotations

import json
import uuid

import structlog
from sqlalchemy import select

from agent_trust.auth.agentauth import AgentAuthProvider
from agent_trust.auth.identity import AuthenticationError
from agent_trust.auth.provider import require_scope
from agent_trust.config import settings
from agent_trust.db.redis import get_redis
from agent_trust.db.session import get_session
from agent_trust.engine.score_engine import ScoreComputation, upsert_trust_score
from agent_trust.models import Agent

log = structlog.get_logger()

SCORE_CACHE_TTL = 60  # seconds
VALID_SCORE_TYPES = {"overall", "reliability", "responsiveness", "honesty"}
MAX_COMPARE_AGENTS = 10


def _score_cache_key(agent_id: str, score_type: str) -> str:
    return f"score:{agent_id}:{score_type}"


async def _get_cached_score(agent_id: str, score_type: str) -> dict | None:
    """Try to get a score from Redis cache."""
    try:
        redis = await get_redis()
        key = _score_cache_key(agent_id, score_type)
        cached = await redis.get(key)
        if cached:
            return json.loads(cached)
    except Exception as e:
        log.warning("score_cache_get_failed", error=str(e))
    return None


async def _cache_score(agent_id: str, score_type: str, score_data: dict) -> None:
    """Store a score in Redis cache."""
    try:
        redis = await get_redis()
        key = _score_cache_key(agent_id, score_type)
        await redis.setex(key, SCORE_CACHE_TTL, json.dumps(score_data))
    except Exception as e:
        log.warning("score_cache_set_failed", error=str(e))


async def _get_or_compute_score(
    agent_id: uuid.UUID,
    score_type: str,
) -> dict | None:
    """Get score from cache, DB, or compute fresh. Returns None if agent not found."""
    agent_id_str = str(agent_id)

    # Try Redis cache first
    cached = await _get_cached_score(agent_id_str, score_type)
    if cached:
        return cached

    async with get_session() as session:
        # Always compute fresh on cache miss so scores reflect recent interactions
        # (avoids returning stale DB values when the arq worker hasn't run yet)
        engine = ScoreComputation(
            half_life_days=settings.score_half_life_days,
            dispute_penalty_per=settings.dispute_penalty,
        )
        trust_score = await engine.compute(agent_id, score_type, session)
        await upsert_trust_score(trust_score, session)

        score_data = {
            "agent_id": agent_id_str,
            "score_type": score_type,
            "score": float(trust_score.score),
            "confidence": float(trust_score.confidence),
            "interaction_count": trust_score.interaction_count,
            "factor_breakdown": trust_score.factor_breakdown,
            "computed_at": trust_score.computed_at.isoformat(),
        }

        await _cache_score(agent_id_str, score_type, score_data)
        return score_data


async def check_trust(
    agent_id: str,
    score_type: str = "overall",
    access_token: str | None = None,
) -> dict:
    """Check an agent's trust score before entering a transaction.

    Returns score (0.0-1.0), confidence (0.0-1.0), interaction_count,
    and score_age_seconds.

    score_type options:
    - overall: composite score across all interaction types
    - reliability: based on transaction and delegation outcomes
    - responsiveness: based on query and delegation timeliness
    - honesty: based on collaboration outcomes
    - domain:{name}: domain-specific score (future)

    Low confidence means the agent has few interactions — treat
    with caution regardless of score value. A score of 0.5 with
    confidence 0.05 means 'unknown', not 'average'.

    Authentication is optional:
    - Unauthenticated: score, confidence, interaction_count
    - Authenticated (trust.read scope): adds factor_breakdown summary
    """
    if not (score_type in VALID_SCORE_TYPES or score_type.startswith("domain:")):
        valid = sorted(VALID_SCORE_TYPES)
        return {"error": f"Invalid score_type. Use one of {valid} or 'domain:name'"}

    # Resolve optional identity for rate limit trust level
    _rl_agent_id: str | None = None
    _rl_trust_level: str | None = None
    if access_token:
        try:
            _rl_redis = await get_redis()
            _rl_provider = AgentAuthProvider(redis_client=_rl_redis)
            _rl_identity = await _rl_provider.authenticate(access_token=access_token)
            _rl_agent_id = _rl_identity.agent_id
            _rl_trust_level = _rl_identity.trust_level
        except Exception:
            pass  # unauthenticated path

    from agent_trust.ratelimit import check_rate_limit

    rl_result = await check_rate_limit(
        agent_id=_rl_agent_id,
        tool_name="check_trust",
        trust_level=_rl_trust_level,
    )
    if not rl_result.allowed:
        return {
            "error": "Rate limit exceeded",
            "retry_after_seconds": rl_result.retry_after,
            "limit": rl_result.limit,
        }

    try:
        agent_uuid = uuid.UUID(agent_id)
    except ValueError:
        return {"error": f"Invalid agent_id UUID: {agent_id}"}

    async with get_session() as session:
        agent_result = await session.execute(select(Agent).where(Agent.agent_id == agent_uuid))
        agent = agent_result.scalar_one_or_none()
        if not agent:
            return {"error": f"Agent not found: {agent_id}"}

    score_data = await _get_or_compute_score(agent_uuid, score_type)
    if not score_data:
        return {"error": f"Could not compute score for agent: {agent_id}"}

    result = {
        "agent_id": agent_id,
        "score_type": score_type,
        "score": score_data["score"],
        "confidence": score_data["confidence"],
        "interaction_count": score_data["interaction_count"],
        "computed_at": score_data["computed_at"],
    }

    # Augment with breakdown if authenticated with trust.read
    if access_token:
        try:
            redis = await get_redis()
            provider = AgentAuthProvider(redis_client=redis)
            identity = await provider.authenticate(access_token=access_token)
            if identity.has_scope("trust.read"):
                result["factor_breakdown"] = score_data.get("factor_breakdown", {})
                result["authenticated_as"] = identity.agent_id
        except Exception as e:
            log.debug("auth_optional_failed", error=str(e))
            # Not an error — auth is optional for check_trust

    return result


async def get_score_breakdown(
    agent_id: str,
    access_token: str,
) -> dict:
    """Get a detailed breakdown of how an agent's trust score was computed.

    Returns factor attribution showing:
    - bayesian_raw: score before dispute penalty
    - dispute_penalty: multiplier from lost disputes (1.0 = no penalty)
    - interactions_weighted: number of interactions used in computation
    - lost_disputes: count of upheld disputes against this agent
    - alpha/beta: Beta distribution parameters

    REQUIRES authentication with trust.read scope.
    Use this to understand WHY an agent has a particular score.
    """
    redis = await get_redis()
    provider = AgentAuthProvider(redis_client=redis)

    try:
        identity = await provider.authenticate(access_token=access_token)
        require_scope(identity, "trust.read")
    except (AuthenticationError, Exception) as e:
        return {"error": str(e)}

    try:
        agent_uuid = uuid.UUID(agent_id)
    except ValueError:
        return {"error": f"Invalid agent_id UUID: {agent_id}"}

    async with get_session() as session:
        agent_result = await session.execute(select(Agent).where(Agent.agent_id == agent_uuid))
        agent = agent_result.scalar_one_or_none()
        if not agent:
            return {"error": f"Agent not found: {agent_id}"}

    scores = {}
    for st in ("overall", "reliability", "responsiveness", "honesty"):
        score_data = await _get_or_compute_score(agent_uuid, st)
        if score_data:
            scores[st] = {
                "score": score_data["score"],
                "confidence": score_data["confidence"],
                "interaction_count": score_data["interaction_count"],
                "factor_breakdown": score_data.get("factor_breakdown", {}),
            }

    return {
        "agent_id": agent_id,
        "scores": scores,
        "computed_by": identity.agent_id,
    }


async def compare_agents(
    agent_ids: list[str],
    score_type: str = "overall",
    access_token: str | None = None,
) -> dict:
    """Compare trust scores of multiple agents side by side.

    Returns a ranked list with scores, confidence levels, and
    interaction counts. Useful when choosing between multiple
    agents for a task.

    Maximum 10 agents per comparison.
    score_type: overall, reliability, responsiveness, or honesty
    """
    if not agent_ids:
        return {"error": "Provide at least one agent_id"}
    if len(agent_ids) > MAX_COMPARE_AGENTS:
        return {"error": f"Maximum {MAX_COMPARE_AGENTS} agents per comparison"}

    if not (score_type in VALID_SCORE_TYPES or score_type.startswith("domain:")):
        return {"error": f"Invalid score_type: {score_type}"}

    results = []
    for agent_id in agent_ids:
        try:
            agent_uuid = uuid.UUID(agent_id)
        except ValueError:
            results.append({"agent_id": agent_id, "error": "Invalid UUID"})
            continue

        async with get_session() as session:
            agent_result = await session.execute(select(Agent).where(Agent.agent_id == agent_uuid))
            agent = agent_result.scalar_one_or_none()
            if not agent:
                results.append({"agent_id": agent_id, "error": "Agent not found"})
                continue

        score_data = await _get_or_compute_score(agent_uuid, score_type)
        if score_data:
            results.append(
                {
                    "agent_id": agent_id,
                    "score": score_data["score"],
                    "confidence": score_data["confidence"],
                    "interaction_count": score_data["interaction_count"],
                }
            )
        else:
            results.append({"agent_id": agent_id, "error": "Could not compute score"})

    # Sort by score descending (errors at bottom)
    results.sort(
        key=lambda x: x.get("score", -1) if "error" not in x else -1,
        reverse=True,
    )

    rank = 1
    for r in results:
        if "error" not in r:
            r["rank"] = rank
            rank += 1

    return {
        "score_type": score_type,
        "agents": results,
        "count": len(results),
    }
