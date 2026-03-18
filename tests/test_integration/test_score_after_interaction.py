from __future__ import annotations

"""Integration tests: trust score changes after reporting interactions.

Covers all four interaction outcomes (success, failure, timeout, partial) and
verifies that BOTH the reporter (initiator) and counterparty scores are
correctly updated with role-aware scoring.

Role-aware scoring rules:
  - Success: reporter gets half credit (α += w×0.5), counterparty gets full credit (α += w)
  - Failure/Timeout: only counterparty is penalized (β += w), reporter unchanged
  - Partial: reporter gets small credit (α += w×0.25), counterparty neutral (α += w×0.5, β += w×0.5)
"""

import uuid
from contextlib import asynccontextmanager
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from agent_trust.engine.score_engine import ScoreComputation
from agent_trust.models import TrustScore
from agent_trust.ratelimit import RateLimitResult
from tests.factories import make_interaction

# New agents start at α=2, β=2 → score = 0.5 with zero interactions
PRIOR_SCORE = 0.5


# ---------------------------------------------------------------------------
# Test helper
# ---------------------------------------------------------------------------


async def _compute(
    agent_id: uuid.UUID,
    interactions: list,
    *,
    lost_disputes: int = 0,
) -> TrustScore:
    """Run ScoreComputation.compute for agent_id using the given interaction list.

    Only DB sub-queries are mocked so the scoring math executes for real:
      - _fetch_interactions   → interactions where agent is initiator OR counterparty
      - _get_cached_score     → 0.5  (reporter has neutral prior, no history)
      - _get_auth_trust_level → "delegated"  (weight = 1.0)
      - _count_lost_disputes  → lost_disputes (0 unless specified)
      - _count_dismissed_*    → 0
      - _get_reporter_interaction_count → 10 (above threshold)
      - _count_mutual_pair_confirmations → 0 (no mutual confirmations)
    """
    engine = ScoreComputation()
    relevant = [
        ix for ix in interactions if ix.initiator_id == agent_id or ix.counterparty_id == agent_id
    ]
    with (
        patch.object(engine, "_fetch_interactions", new=AsyncMock(return_value=relevant)),
        patch.object(engine, "_get_cached_score", new=AsyncMock(return_value=0.5)),
        patch.object(engine, "_get_auth_trust_level", new=AsyncMock(return_value="delegated")),
        patch.object(engine, "_count_lost_disputes", new=AsyncMock(return_value=lost_disputes)),
        patch.object(
            engine,
            "_count_dismissed_disputes_filed_by",
            new=AsyncMock(return_value=0),
        ),
        patch.object(
            engine,
            "_get_reporter_interaction_count",
            new=AsyncMock(return_value=10),
        ),
        patch.object(
            engine,
            "_count_mutual_pair_confirmations",
            new=AsyncMock(return_value=0),
        ),
        patch(
            "agent_trust.engine.score_engine.get_sybil_credibility_multiplier",
            new=AsyncMock(return_value=1.0),
        ),
    ):
        return await engine.compute(agent_id, "overall", AsyncMock())


# ---------------------------------------------------------------------------
# success outcome
# ---------------------------------------------------------------------------


class TestSuccessOutcome:
    @pytest.mark.asyncio
    async def test_reporter_score_increases(self):
        """Reporter's score rises above the prior after a reported success."""
        reporter_id = uuid.uuid4()
        cp_id = uuid.uuid4()
        ix = make_interaction(reporter_id, cp_id, outcome="success")

        score = await _compute(reporter_id, [ix])

        assert score.score > PRIOR_SCORE
        assert score.interaction_count == 1

    @pytest.mark.asyncio
    async def test_counterparty_score_increases(self):
        """Counterparty score also rises — both parties benefit from a mutual success."""
        reporter_id = uuid.uuid4()
        cp_id = uuid.uuid4()
        ix = make_interaction(reporter_id, cp_id, outcome="success")

        score = await _compute(cp_id, [ix])

        assert score.score > PRIOR_SCORE
        assert score.interaction_count == 1

    @pytest.mark.asyncio
    async def test_both_scores_increase_asymmetrically(self):
        """Reporter gets half credit, counterparty gets full credit from success.

        Role-aware: reporter (α += w×0.5), counterparty (α += w).
        Both scores increase above prior, but counterparty score is higher.
        """
        reporter_id = uuid.uuid4()
        cp_id = uuid.uuid4()
        ix = make_interaction(reporter_id, cp_id, outcome="success")

        reporter_score = await _compute(reporter_id, [ix])
        cp_score = await _compute(cp_id, [ix])

        assert reporter_score.score > PRIOR_SCORE
        assert cp_score.score > PRIOR_SCORE
        assert reporter_score.score < cp_score.score


# ---------------------------------------------------------------------------
# failure outcome
# ---------------------------------------------------------------------------


class TestFailureOutcome:
    @pytest.mark.asyncio
    async def test_counterparty_score_decreases(self):
        """Counterparty score drops below the prior after a reported failure."""
        reporter_id = uuid.uuid4()
        cp_id = uuid.uuid4()
        ix = make_interaction(reporter_id, cp_id, outcome="failure")

        score = await _compute(cp_id, [ix])

        assert score.score < PRIOR_SCORE

    @pytest.mark.asyncio
    async def test_reporter_score_unchanged(self):
        """Reporter is not penalized for reporting failure — score stays at prior.

        Role-aware: only counterparty gets β += w, reporter unchanged.
        """
        reporter_id = uuid.uuid4()
        cp_id = uuid.uuid4()
        ix = make_interaction(reporter_id, cp_id, outcome="failure")

        score = await _compute(reporter_id, [ix])

        assert score.score == pytest.approx(PRIOR_SCORE, abs=1e-4)

    @pytest.mark.asyncio
    async def test_scores_diverge_asymmetrically(self):
        """Reporter stays at prior, counterparty drops — role-aware asymmetry.

        Role-aware: reporter unchanged, counterparty penalized (β += w).
        """
        reporter_id = uuid.uuid4()
        cp_id = uuid.uuid4()
        ix = make_interaction(reporter_id, cp_id, outcome="failure")

        reporter_score = await _compute(reporter_id, [ix])
        cp_score = await _compute(cp_id, [ix])

        assert reporter_score.score == pytest.approx(PRIOR_SCORE, abs=1e-4)
        assert cp_score.score < PRIOR_SCORE
        assert reporter_score.score > cp_score.score

    @pytest.mark.asyncio
    async def test_failure_lower_than_success(self):
        """A failure produces a strictly lower score than a success."""
        reporter_id = uuid.uuid4()
        cp_id = uuid.uuid4()

        success_ix = make_interaction(reporter_id, cp_id, outcome="success")
        failure_ix = make_interaction(reporter_id, cp_id, outcome="failure")

        success_score = await _compute(cp_id, [success_ix])
        failure_score = await _compute(cp_id, [failure_ix])

        assert failure_score.score < success_score.score


# ---------------------------------------------------------------------------
# timeout outcome
# ---------------------------------------------------------------------------


class TestTimeoutOutcome:
    @pytest.mark.asyncio
    async def test_counterparty_score_decreases(self):
        """Timeout is treated identically to failure — counterparty score drops."""
        reporter_id = uuid.uuid4()
        cp_id = uuid.uuid4()
        ix = make_interaction(reporter_id, cp_id, outcome="timeout")

        score = await _compute(cp_id, [ix])

        assert score.score < PRIOR_SCORE

    @pytest.mark.asyncio
    async def test_reporter_score_unchanged(self):
        """Reporter is not penalized for reporting timeout — score stays at prior.

        Role-aware: only counterparty gets β += w, reporter unchanged.
        """
        reporter_id = uuid.uuid4()
        cp_id = uuid.uuid4()
        ix = make_interaction(reporter_id, cp_id, outcome="timeout")

        score = await _compute(reporter_id, [ix])

        assert score.score == pytest.approx(PRIOR_SCORE, abs=1e-4)

    @pytest.mark.asyncio
    async def test_timeout_same_weight_as_failure(self):
        """Timeout and failure carry identical weight — both increment β by the same amount."""
        reporter_id = uuid.uuid4()
        cp_id = uuid.uuid4()

        timeout_ix = make_interaction(reporter_id, cp_id, outcome="timeout")
        failure_ix = make_interaction(reporter_id, cp_id, outcome="failure")

        timeout_score = await _compute(cp_id, [timeout_ix])
        failure_score = await _compute(cp_id, [failure_ix])

        assert timeout_score.score == pytest.approx(failure_score.score)


# ---------------------------------------------------------------------------
# partial outcome
# ---------------------------------------------------------------------------


class TestPartialOutcome:
    @pytest.mark.asyncio
    async def test_counterparty_score_stays_neutral(self):
        """Partial adds equally to α and β, leaving the score at exactly 0.5."""
        reporter_id = uuid.uuid4()
        cp_id = uuid.uuid4()
        ix = make_interaction(reporter_id, cp_id, outcome="partial")

        score = await _compute(cp_id, [ix])

        assert score.score == pytest.approx(PRIOR_SCORE, abs=1e-4)

    @pytest.mark.asyncio
    async def test_reporter_score_increases_slightly(self):
        """Reporter gets small credit for partial — score rises slightly above prior.

        Role-aware: reporter gets α += w×0.25, no β change.
        With α=2, β=2, weight=0.6: α becomes 2.15, score = 2.15/4.15 ≈ 0.518.
        """
        reporter_id = uuid.uuid4()
        cp_id = uuid.uuid4()
        ix = make_interaction(reporter_id, cp_id, outcome="partial")

        score = await _compute(reporter_id, [ix])

        assert score.score > PRIOR_SCORE

    @pytest.mark.asyncio
    async def test_confidence_increases(self):
        """A partial outcome is still counted as an interaction, increasing confidence."""
        reporter_id = uuid.uuid4()
        cp_id = uuid.uuid4()
        ix = make_interaction(reporter_id, cp_id, outcome="partial")

        score = await _compute(cp_id, [ix])

        assert score.interaction_count == 1

    @pytest.mark.asyncio
    async def test_partial_weaker_direction_than_success(self):
        """Success moves score above 0.5; partial stays at 0.5."""
        reporter_id = uuid.uuid4()
        cp_id = uuid.uuid4()

        success_ix = make_interaction(reporter_id, cp_id, outcome="success")
        partial_ix = make_interaction(reporter_id, cp_id, outcome="partial")

        success_score = await _compute(cp_id, [success_ix])
        partial_score = await _compute(cp_id, [partial_ix])

        assert success_score.score > partial_score.score

    @pytest.mark.asyncio
    async def test_partial_weaker_direction_than_failure(self):
        """Failure moves score below 0.5; partial stays at 0.5."""
        reporter_id = uuid.uuid4()
        cp_id = uuid.uuid4()

        failure_ix = make_interaction(reporter_id, cp_id, outcome="failure")
        partial_ix = make_interaction(reporter_id, cp_id, outcome="partial")

        failure_score = await _compute(cp_id, [failure_ix])
        partial_score = await _compute(cp_id, [partial_ix])

        assert failure_score.score < partial_score.score


# ---------------------------------------------------------------------------
# mutual confirmation
# ---------------------------------------------------------------------------


class TestMutualConfirmation:
    @pytest.mark.asyncio
    async def test_confirmed_success_scores_higher(self):
        """mutually_confirmed=True gives a 1.5× weight bonus, raising the score further."""
        reporter_id = uuid.uuid4()
        cp_id = uuid.uuid4()

        unconfirmed = make_interaction(
            reporter_id, cp_id, outcome="success", mutually_confirmed=False
        )
        confirmed = make_interaction(reporter_id, cp_id, outcome="success", mutually_confirmed=True)

        unconfirmed_score = await _compute(cp_id, [unconfirmed])
        confirmed_score = await _compute(cp_id, [confirmed])

        assert confirmed_score.score > unconfirmed_score.score

    @pytest.mark.asyncio
    async def test_confirmed_failure_scores_lower(self):
        """The 1.5× bonus also amplifies failure — a mutually confirmed failure hurts more."""
        reporter_id = uuid.uuid4()
        cp_id = uuid.uuid4()

        unconfirmed = make_interaction(
            reporter_id, cp_id, outcome="failure", mutually_confirmed=False
        )
        confirmed = make_interaction(reporter_id, cp_id, outcome="failure", mutually_confirmed=True)

        unconfirmed_score = await _compute(cp_id, [unconfirmed])
        confirmed_score = await _compute(cp_id, [confirmed])

        assert confirmed_score.score < unconfirmed_score.score

    @pytest.mark.asyncio
    async def test_confirmed_partial_still_neutral(self):
        """Even with the 1.5× bonus, a partial stays at 0.5 (equal α/β increment)."""
        reporter_id = uuid.uuid4()
        cp_id = uuid.uuid4()
        ix = make_interaction(reporter_id, cp_id, outcome="partial", mutually_confirmed=True)

        score = await _compute(cp_id, [ix])

        assert score.score == pytest.approx(PRIOR_SCORE, abs=1e-4)


# ---------------------------------------------------------------------------
# multiple interactions
# ---------------------------------------------------------------------------


class TestMultipleInteractions:
    @pytest.mark.asyncio
    async def test_multiple_successes_compound(self):
        """Three successes push the score noticeably higher than a single success."""
        reporter_id = uuid.uuid4()
        cp_id = uuid.uuid4()

        one = [make_interaction(reporter_id, cp_id, outcome="success")]
        three = [make_interaction(reporter_id, cp_id, outcome="success") for _ in range(3)]

        score_one = await _compute(cp_id, one)
        score_three = await _compute(cp_id, three)

        assert score_three.score > score_one.score

    @pytest.mark.asyncio
    async def test_multiple_failures_compound(self):
        """Three failures push the score noticeably lower than a single failure."""
        reporter_id = uuid.uuid4()
        cp_id = uuid.uuid4()

        one = [make_interaction(reporter_id, cp_id, outcome="failure")]
        three = [make_interaction(reporter_id, cp_id, outcome="failure") for _ in range(3)]

        score_one = await _compute(cp_id, one)
        score_three = await _compute(cp_id, three)

        assert score_three.score < score_one.score

    @pytest.mark.asyncio
    async def test_successes_outweigh_single_failure(self):
        """Three successes recover the score above the prior after one failure."""
        reporter_id = uuid.uuid4()
        cp_id = uuid.uuid4()

        interactions = [
            make_interaction(reporter_id, cp_id, outcome="failure"),
            make_interaction(reporter_id, cp_id, outcome="success"),
            make_interaction(reporter_id, cp_id, outcome="success"),
            make_interaction(reporter_id, cp_id, outcome="success"),
        ]

        score = await _compute(cp_id, interactions)

        assert score.score > PRIOR_SCORE

    @pytest.mark.asyncio
    async def test_interaction_count_reflects_all(self):
        """interaction_count equals the number of relevant interactions found."""
        reporter_id = uuid.uuid4()
        cp_id = uuid.uuid4()
        n = 5
        interactions = [make_interaction(reporter_id, cp_id, outcome="success") for _ in range(n)]

        score = await _compute(cp_id, interactions)

        assert score.interaction_count == n

    @pytest.mark.asyncio
    async def test_unrelated_interactions_excluded(self):
        """Interactions between other agents do not affect the agent's score."""
        reporter_id = uuid.uuid4()
        cp_id = uuid.uuid4()
        other_a = uuid.uuid4()
        other_b = uuid.uuid4()

        relevant = make_interaction(reporter_id, cp_id, outcome="success")
        irrelevant = make_interaction(other_a, other_b, outcome="failure")

        score_with = await _compute(cp_id, [relevant, irrelevant])
        score_without = await _compute(cp_id, [relevant])

        # irrelevant interaction should have zero effect
        assert score_with.score == pytest.approx(score_without.score)
        assert score_with.interaction_count == score_without.interaction_count


# ---------------------------------------------------------------------------
# check_trust: always computes fresh (regression guard for stale-DB bug)
# ---------------------------------------------------------------------------


class TestCheckTrustFreshComputation:
    @pytest.mark.asyncio
    async def test_stale_db_score_not_returned(self):
        """check_trust computes a fresh score on cache miss, ignoring any stale DB value.

        Regression test: previously check_trust returned a stale DB entry for the
        counterparty even after new interactions were recorded, because _get_or_compute_score
        returned DB rows without recomputing. The fix always runs ScoreComputation.compute
        on a cache miss.
        """
        from agent_trust.tools.scoring import check_trust

        agent_uuid = uuid.uuid4()
        agent_id_str = str(agent_uuid)
        reporter_id = uuid.uuid4()

        ix = make_interaction(reporter_id, agent_uuid, outcome="success")

        # Simulate the stale DB state: score exists from before the interaction was recorded
        stale_db_score = TrustScore(
            agent_id=agent_uuid,
            score_type="overall",
            score=PRIOR_SCORE,
            confidence=0.0,
            interaction_count=0,
            factor_breakdown={},
        )

        mock_agent = MagicMock()
        mock_agent.agent_id = agent_uuid

        # Fresh ScoreComputation that finds the new interaction
        real_engine = ScoreComputation()
        with (
            patch.object(real_engine, "_fetch_interactions", new=AsyncMock(return_value=[ix])),
            patch.object(real_engine, "_get_cached_score", new=AsyncMock(return_value=0.5)),
            patch.object(
                real_engine, "_get_auth_trust_level", new=AsyncMock(return_value="delegated")
            ),
            patch.object(real_engine, "_count_lost_disputes", new=AsyncMock(return_value=0)),
            patch.object(
                real_engine,
                "_count_dismissed_disputes_filed_by",
                new=AsyncMock(return_value=0),
            ),
            patch.object(
                real_engine,
                "_get_reporter_interaction_count",
                new=AsyncMock(return_value=10),
            ),
            patch.object(
                real_engine,
                "_count_mutual_pair_confirmations",
                new=AsyncMock(return_value=0),
            ),
            patch(
                "agent_trust.engine.score_engine.get_sybil_credibility_multiplier",
                new=AsyncMock(return_value=1.0),
            ),
        ):

            @asynccontextmanager
            async def fake_session():
                session = AsyncMock()

                async def execute(*args, **kwargs):
                    r = MagicMock()
                    r.scalar_one_or_none.return_value = mock_agent
                    r.scalar.return_value = 0  # For sybil detector burst count queries
                    return r

                session.execute = execute
                yield session

            redis_mock = AsyncMock()
            redis_mock.get = AsyncMock(return_value=None)  # cache miss → must compute fresh
            redis_mock.setex = AsyncMock()

            with (
                patch("agent_trust.tools.scoring.get_session", side_effect=fake_session),
                patch(
                    "agent_trust.tools.scoring.get_redis", new=AsyncMock(return_value=redis_mock)
                ),
                patch("agent_trust.tools.scoring.ScoreComputation", return_value=real_engine),
                patch("agent_trust.tools.scoring.upsert_trust_score", new=AsyncMock()),
                patch(
                    "agent_trust.ratelimit.check_rate_limit",
                    new=AsyncMock(
                        return_value=RateLimitResult(
                            allowed=True, limit=60, remaining=59, reset_at=9_999_999_999
                        )
                    ),
                ),
            ):
                result = await check_trust(agent_id=agent_id_str)

        assert "error" not in result
        # Fresh computation found the interaction → score above prior, not the stale 0.5
        assert result["score"] > stale_db_score.score
        assert result["interaction_count"] == 1

    @pytest.mark.asyncio
    async def test_cache_hit_skips_recomputation(self):
        """A Redis cache hit is returned directly without recomputing."""
        import json
        from datetime import UTC, datetime

        from agent_trust.tools.scoring import check_trust

        agent_uuid = uuid.uuid4()

        cached_data = {
            "agent_id": str(agent_uuid),
            "score_type": "overall",
            "score": 0.88,
            "confidence": 0.95,
            "interaction_count": 15,
            "factor_breakdown": {},
            "computed_at": datetime.now(UTC).isoformat(),
        }

        mock_agent = MagicMock()
        mock_agent.agent_id = agent_uuid

        @asynccontextmanager
        async def fake_session():
            session = AsyncMock()

            async def execute(*args, **kwargs):
                r = MagicMock()
                r.scalar_one_or_none.return_value = mock_agent
                return r

            session.execute = execute
            yield session

        redis_mock = AsyncMock()
        redis_mock.get = AsyncMock(return_value=json.dumps(cached_data))  # cache hit
        redis_mock.setex = AsyncMock()

        with (
            patch("agent_trust.tools.scoring.get_session", side_effect=fake_session),
            patch("agent_trust.tools.scoring.get_redis", new=AsyncMock(return_value=redis_mock)),
            patch(
                "agent_trust.ratelimit.check_rate_limit",
                new=AsyncMock(
                    return_value=RateLimitResult(
                        allowed=True, limit=60, remaining=59, reset_at=9_999_999_999
                    )
                ),
            ),
        ):
            result = await check_trust(agent_id=str(agent_uuid))

        assert result["score"] == 0.88
        assert result["interaction_count"] == 15
        # Cache was hit — setex must NOT have been called (no recompute)
        redis_mock.setex.assert_not_called()


# ---------------------------------------------------------------------------
# role-aware scoring asymmetries
# ---------------------------------------------------------------------------


class TestRoleAwareScoring:
    """Tests verifying role-aware scoring asymmetries between reporter and counterparty."""

    @pytest.mark.asyncio
    async def test_reporter_not_penalized_for_failure(self):
        """Reporter reports failure but their score stays at prior (no penalty).

        Role-aware: only counterparty is penalized for failure, not the reporter.
        """
        reporter_id = uuid.uuid4()
        cp_id = uuid.uuid4()
        ix = make_interaction(reporter_id, cp_id, outcome="failure")

        reporter_score = await _compute(reporter_id, [ix])
        cp_score = await _compute(cp_id, [ix])

        assert reporter_score.score == pytest.approx(PRIOR_SCORE, abs=1e-4)
        assert cp_score.score < PRIOR_SCORE
        assert reporter_score.interaction_count == 1

    @pytest.mark.asyncio
    async def test_reporter_not_penalized_for_timeout(self):
        """Reporter reports timeout but their score stays at prior (no penalty).

        Role-aware: timeout treated like failure — only counterparty penalized.
        """
        reporter_id = uuid.uuid4()
        cp_id = uuid.uuid4()
        ix = make_interaction(reporter_id, cp_id, outcome="timeout")

        reporter_score = await _compute(reporter_id, [ix])
        cp_score = await _compute(cp_id, [ix])

        assert reporter_score.score == pytest.approx(PRIOR_SCORE, abs=1e-4)
        assert cp_score.score < PRIOR_SCORE

    @pytest.mark.asyncio
    async def test_reporter_gets_half_credit_for_success(self):
        """Reporter gets half credit (α += w×0.5), counterparty gets full (α += w).

        Role-aware: success benefits both, but counterparty more than reporter.
        """
        reporter_id = uuid.uuid4()
        cp_id = uuid.uuid4()
        ix = make_interaction(reporter_id, cp_id, outcome="success")

        reporter_score = await _compute(reporter_id, [ix])
        cp_score = await _compute(cp_id, [ix])

        assert reporter_score.score > PRIOR_SCORE
        assert cp_score.score > PRIOR_SCORE
        assert reporter_score.score < cp_score.score

    @pytest.mark.asyncio
    async def test_reporter_gets_small_credit_for_partial(self):
        """Reporter gets α += w×0.25 for partial, slightly above prior.

        Role-aware: reporter gets small participation credit, counterparty stays neutral.
        """
        reporter_id = uuid.uuid4()
        cp_id = uuid.uuid4()
        ix = make_interaction(reporter_id, cp_id, outcome="partial")

        reporter_score = await _compute(reporter_id, [ix])
        cp_score = await _compute(cp_id, [ix])

        assert reporter_score.score > PRIOR_SCORE
        assert cp_score.score == pytest.approx(PRIOR_SCORE, abs=1e-4)
        assert reporter_score.score > cp_score.score  # reporter slightly above, cp at 0.5

    @pytest.mark.asyncio
    async def test_counterparty_fully_penalized_for_failure(self):
        """Counterparty receives full penalty (β += w) for failure while reporter unchanged.

        Role-aware: failure scoring is asymmetric — only the counterparty is blamed.
        """
        reporter_id = uuid.uuid4()
        cp_id = uuid.uuid4()

        success_ix = make_interaction(reporter_id, cp_id, outcome="success")
        failure_ix = make_interaction(reporter_id, cp_id, outcome="failure")

        # Counterparty: success raises score, failure lowers it
        success_score = await _compute(cp_id, [success_ix])
        failure_score = await _compute(cp_id, [failure_ix])

        assert success_score.score > PRIOR_SCORE
        assert failure_score.score < PRIOR_SCORE

        # Reporter: success raises score (half credit), failure unchanged
        reporter_success = await _compute(reporter_id, [success_ix])
        reporter_failure = await _compute(reporter_id, [failure_ix])

        assert reporter_success.score > PRIOR_SCORE
        assert reporter_failure.score == pytest.approx(PRIOR_SCORE, abs=1e-4)

    @pytest.mark.asyncio
    async def test_role_reversal_changes_scoring(self):
        """When roles are reversed, the scoring asymmetry flips.

        Agent A reports failure of B: A unchanged, B penalized.
        Agent B reports failure of A: B unchanged, A penalized.
        """
        agent_a = uuid.uuid4()
        agent_b = uuid.uuid4()

        # A reports failure of B
        ix1 = make_interaction(agent_a, agent_b, outcome="failure", reported_by=agent_a)

        # B reports failure of A (role reversed)
        ix2 = make_interaction(agent_b, agent_a, outcome="failure", reported_by=agent_b)

        # After ix1: A unchanged (reporter), B penalized (counterparty)
        score_a_after_ix1 = await _compute(agent_a, [ix1])
        score_b_after_ix1 = await _compute(agent_b, [ix1])

        assert score_a_after_ix1.score == pytest.approx(PRIOR_SCORE, abs=1e-4)
        assert score_b_after_ix1.score < PRIOR_SCORE

        # After ix2: B unchanged (reporter), A penalized (counterparty)
        score_a_after_ix2 = await _compute(agent_a, [ix2])
        score_b_after_ix2 = await _compute(agent_b, [ix2])

        assert score_b_after_ix2.score == pytest.approx(PRIOR_SCORE, abs=1e-4)
        assert score_a_after_ix2.score < PRIOR_SCORE
