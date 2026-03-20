from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

import structlog
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from agent_trust.engine.sybil_detector import get_sybil_credibility_multiplier
from agent_trust.models import Agent, Dispute, Interaction, TrustScore

log = structlog.get_logger()

SCORE_TYPES = {"overall", "reliability", "responsiveness", "honesty"}

# Map interaction types to score dimensions
INTERACTION_TYPE_SCORE_MAP = {
    "transaction": {"overall", "reliability"},
    "delegation": {"overall", "reliability", "responsiveness"},
    "query": {"overall", "responsiveness"},
    "collaboration": {"overall", "honesty", "reliability"},
}

# AgentAuth trust level weights for reporter credibility
TRUST_LEVEL_WEIGHTS: dict[str, float] = {
    "root": 1.2,
    "delegated": 1.0,
    "ephemeral": 0.7,
    "standalone": 0.8,
}


@dataclass
class ScoreComputation:
    """Bayesian trust score computation engine.

    Uses Beta distribution priors with time-decayed, credibility-weighted
    interactions. Reporter credibility is based on their own trust score
    and their AgentAuth trust level.
    """

    prior_alpha: float = 2.0
    prior_beta: float = 2.0
    half_life_days: float = 90.0
    dispute_penalty_per: float = 0.03
    dispute_penalty_floor: float = 0.50
    dismissed_penalty_per: float = 0.01
    dismissed_penalty_floor: float = 0.90
    mutual_confirmation_bonus: float = 1.5
    trust_level_weights: dict[str, float] = field(default_factory=lambda: dict(TRUST_LEVEL_WEIGHTS))

    async def compute(
        self,
        agent_id: uuid.UUID,
        score_type: str,
        session: AsyncSession,
    ) -> TrustScore:
        """Compute trust score for an agent using all available interactions.

        Returns a TrustScore with score, confidence, interaction_count,
        and factor_breakdown showing Bayesian components.
        """
        interactions = await self._fetch_interactions(agent_id, score_type, session)

        alpha, beta = self.prior_alpha, self.prior_beta
        now = datetime.now(UTC)

        # Caches for per-reporter and per-pair computations
        sybil_cache: dict[uuid.UUID, float] = {}
        mutual_pair_cache: dict[tuple[uuid.UUID, uuid.UUID], int] = {}

        for ix in interactions:
            age_days = (now - ix.reported_at).total_seconds() / 86400
            time_weight = 0.5 ** (age_days / self.half_life_days)

            reporter_trust = await self._get_cached_score(ix.reported_by, session)
            reporter_auth_level = await self._get_auth_trust_level(ix.reported_by, session)
            level_weight = self.trust_level_weights.get(reporter_auth_level, 0.8)
            credibility = (0.5 + reporter_trust * 0.5) * level_weight

            # Fix #1: Apply sybil credibility multiplier
            if ix.reported_by not in sybil_cache:
                sybil_cache[ix.reported_by] = await get_sybil_credibility_multiplier(
                    ix.reported_by, session
                )
            credibility *= sybil_cache[ix.reported_by]

            # Fix #5: Low-interaction reporters get reduced weight
            reporter_interaction_count = await self._get_reporter_interaction_count(
                ix.reported_by, session
            )
            if reporter_interaction_count < 3:
                credibility *= 0.3

            # Fix #2: Mutual confirmation with diminishing returns
            if ix.mutually_confirmed:
                pair_key = (min(agent_id, ix.reported_by), max(agent_id, ix.reported_by))
                if pair_key not in mutual_pair_cache:
                    mutual_pair_cache[pair_key] = await self._count_mutual_pair_confirmations(
                        agent_id, ix.reported_by, session
                    )
                pair_count = mutual_pair_cache[pair_key]
                mutual = max(1.5 - 0.1 * max(0, pair_count - 1), 1.0)
            else:
                mutual = 1.0

            w = time_weight * credibility * mutual

            is_reporter = ix.reported_by == agent_id

            if ix.outcome == "success":
                if is_reporter:
                    alpha += w * 0.5
                else:
                    alpha += w
            elif ix.outcome in ("failure", "timeout"):
                if not is_reporter:
                    beta += w
            elif ix.outcome == "partial":
                if is_reporter:
                    alpha += w * 0.25
                else:
                    alpha += w * 0.5
                    beta += w * 0.5

        lost_disputes = await self._count_lost_disputes(agent_id, session)
        penalty = max(
            1.0 - lost_disputes * self.dispute_penalty_per,
            self.dispute_penalty_floor,
        )

        raw_score = alpha / (alpha + beta)
        score = raw_score * penalty

        dismissed_filed = await self._count_dismissed_disputes_filed_by(agent_id, session)
        # Fix #6: Exponential dismissed-dispute penalty
        if dismissed_filed == 0:
            dismissed_penalty = 1.0
        else:
            total_reduction = sum(
                self.dismissed_penalty_per * (1.5**i) for i in range(dismissed_filed)
            )
            dismissed_penalty = max(1.0 - total_reduction, self.dismissed_penalty_floor)
        score = score * dismissed_penalty
        score = round(min(max(score, 0.0), 1.0), 4)

        n = len(interactions)
        confidence = 1.0 - (1.0 / (1.0 + n * 0.1))

        return TrustScore(
            agent_id=agent_id,
            score_type=score_type,
            score=score,
            confidence=round(min(max(confidence, 0.0), 1.0), 4),
            interaction_count=n,
            factor_breakdown={
                "bayesian_raw": round(raw_score, 4),
                "dispute_penalty": round(penalty, 4),
                "interactions_weighted": n,
                "lost_disputes": lost_disputes,
                "dismissed_disputes_filed": dismissed_filed,
                "dismissed_penalty": round(dismissed_penalty, 4),
                "alpha": round(alpha, 4),
                "beta": round(beta, 4),
            },
            computed_at=now,
        )

    async def compute_all_types(
        self,
        agent_id: uuid.UUID,
        session: AsyncSession,
    ) -> list[TrustScore]:
        """Compute scores for all score types and return them all."""
        results = []
        for score_type in SCORE_TYPES:
            score = await self.compute(agent_id, score_type, session)
            results.append(score)
        return results

    async def _fetch_interactions(
        self,
        agent_id: uuid.UUID,
        score_type: str,
        session: AsyncSession,
    ) -> list[Interaction]:
        """Fetch interactions relevant to this score type for this agent."""
        from sqlalchemy import or_

        query = select(Interaction).where(
            or_(
                Interaction.initiator_id == agent_id,
                Interaction.counterparty_id == agent_id,
            )
        )

        if score_type != "overall":
            relevant_types = [
                itype
                for itype, score_set in INTERACTION_TYPE_SCORE_MAP.items()
                if score_type in score_set
            ]
            if relevant_types:
                query = query.where(Interaction.interaction_type.in_(relevant_types))

        result = await session.execute(query)
        return list(result.scalars().all())

    async def _get_cached_score(
        self,
        agent_id: uuid.UUID,
        session: AsyncSession,
    ) -> float:
        """Get an agent's cached overall score (default 0.5 if not computed yet)."""
        result = await session.execute(
            select(TrustScore).where(
                TrustScore.agent_id == agent_id,
                TrustScore.score_type == "overall",
            )
        )
        score_row = result.scalar_one_or_none()
        if score_row:
            return float(score_row.score)
        return 0.5

    async def _get_auth_trust_level(
        self,
        agent_id: uuid.UUID,
        session: AsyncSession,
    ) -> str:
        """Get an agent's auth trust level from their profile.

        Fix #8: Trust level is derived ONLY from auth_source and agentauth_linked,
        never from user-supplied metadata (prevents trust_level spoofing).
        """
        result = await session.execute(select(Agent).where(Agent.agent_id == agent_id))
        agent = result.scalar_one_or_none()
        if not agent:
            return "ephemeral"
        if agent.auth_source == "agentauth" and agent.agentauth_linked:
            return "delegated"
        elif agent.auth_source == "agentauth" and not agent.agentauth_linked:
            return "ephemeral"
        elif agent.auth_source == "standalone":
            return "standalone"
        return "ephemeral"

    async def _count_lost_disputes(
        self,
        agent_id: uuid.UUID,
        session: AsyncSession,
    ) -> int:
        """Count disputes upheld against this agent."""
        result = await session.execute(
            select(func.count())
            .select_from(Dispute)
            .where(
                Dispute.filed_against == agent_id,
                Dispute.status == "resolved",
                Dispute.resolution == "upheld",
            )
        )
        return result.scalar() or 0

    async def _count_dismissed_disputes_filed_by(
        self,
        agent_id: uuid.UUID,
        session: AsyncSession,
    ) -> int:
        """Count frivolous disputes dismissed against this agent as filer."""
        result = await session.execute(
            select(func.count())
            .select_from(Dispute)
            .where(
                Dispute.filed_by == agent_id,
                Dispute.status == "resolved",
                Dispute.resolution == "dismissed",
            )
        )
        return result.scalar() or 0

    async def _get_reporter_interaction_count(
        self,
        agent_id: uuid.UUID,
        session: AsyncSession,
    ) -> int:
        """Get the interaction count for a reporter from their overall trust score."""
        result = await session.execute(
            select(TrustScore).where(
                TrustScore.agent_id == agent_id,
                TrustScore.score_type == "overall",
            )
        )
        score_row = result.scalar_one_or_none()
        return score_row.interaction_count if score_row else 0

    async def _count_mutual_pair_confirmations(
        self,
        agent_id: uuid.UUID,
        reporter_id: uuid.UUID,
        session: AsyncSession,
    ) -> int:
        """Count mutually confirmed interactions between this pair in last 30 days."""
        cutoff = datetime.now(UTC) - timedelta(days=30)
        result = await session.execute(
            select(func.count())
            .select_from(Interaction)
            .where(
                Interaction.mutually_confirmed == True,  # noqa: E712
                Interaction.reported_at >= cutoff,
                (
                    (Interaction.initiator_id == agent_id)
                    & (Interaction.counterparty_id == reporter_id)
                )
                | (
                    (Interaction.initiator_id == reporter_id)
                    & (Interaction.counterparty_id == agent_id)
                ),
            )
        )
        return result.scalar() or 0


def explain_score(score: float, confidence: float, breakdown: dict) -> str:
    """Generate a human/LLM-readable explanation of a trust score.

    Summarizes the key factors behind the score in plain language,
    helping agents reason about whether to trust a counterparty.
    """
    parts: list[str] = []

    n = breakdown.get("interactions_weighted", 0)

    # Overall assessment
    if confidence < 0.15:
        parts.append(
            f"Score {score:.2f} with very low confidence ({confidence:.2f}) — "
            "too few interactions to draw conclusions. Treat as 'unknown'."
        )
    elif score >= 0.8:
        parts.append(f"High trust score ({score:.2f}) with {n} interaction(s).")
    elif score >= 0.6:
        parts.append(f"Moderate trust score ({score:.2f}) with {n} interaction(s).")
    elif score >= 0.4:
        parts.append(f"Below-average trust score ({score:.2f}) with {n} interaction(s).")
    else:
        parts.append(f"Low trust score ({score:.2f}) with {n} interaction(s).")

    # Dispute impact
    lost = breakdown.get("lost_disputes", 0)
    penalty = breakdown.get("dispute_penalty", 1.0)
    if lost > 0:
        parts.append(f"{lost} upheld dispute(s) apply a {(1.0 - penalty):.0%} penalty.")

    dismissed = breakdown.get("dismissed_disputes_filed", 0)
    dismissed_penalty = breakdown.get("dismissed_penalty", 1.0)
    if dismissed > 0:
        parts.append(
            f"Filed {dismissed} dismissed dispute(s), reducing own score by "
            f"{(1.0 - dismissed_penalty):.1%}."
        )

    # Bayesian factors
    alpha = breakdown.get("alpha", 2.0)
    beta = breakdown.get("beta", 2.0)
    if alpha > 2.0 and beta <= 2.5:
        parts.append("Predominantly positive interactions.")
    elif beta > alpha:
        parts.append("More negative than positive interactions weighted by recency.")

    return " ".join(parts)


async def upsert_trust_score(
    trust_score: TrustScore,
    session: AsyncSession,
) -> None:
    """Insert or update a trust score in the database."""
    existing = await session.execute(
        select(TrustScore).where(
            TrustScore.agent_id == trust_score.agent_id,
            TrustScore.score_type == trust_score.score_type,
        )
    )
    row = existing.scalar_one_or_none()
    if row:
        row.score = trust_score.score
        row.confidence = trust_score.confidence
        row.interaction_count = trust_score.interaction_count
        row.factor_breakdown = trust_score.factor_breakdown
        row.computed_at = trust_score.computed_at
    else:
        session.add(trust_score)
