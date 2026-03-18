from __future__ import annotations

import uuid
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from hypothesis import given
from hypothesis import settings as h_settings
from hypothesis import strategies as st

from agent_trust.engine.score_engine import TRUST_LEVEL_WEIGHTS, ScoreComputation
from tests.factories import make_interaction

# Patch target for sybil multiplier
_SYBIL_PATCH = "agent_trust.engine.score_engine.get_sybil_credibility_multiplier"


def _setup_sybil_mocks(engine: ScoreComputation) -> None:
    """Set up mocks for the new security-related helper methods."""
    engine._get_reporter_interaction_count = AsyncMock(return_value=10)
    engine._count_mutual_pair_confirmations = AsyncMock(return_value=0)


@pytest.fixture
def engine() -> ScoreComputation:
    return ScoreComputation()


@pytest.mark.asyncio
async def test_zero_interactions_returns_neutral_score(engine: ScoreComputation) -> None:
    """Zero interactions → score near 0.5 with low confidence."""
    agent_id = uuid.uuid4()
    mock_session = AsyncMock()

    mock_result = MagicMock()
    mock_result.scalars.return_value.all.return_value = []
    mock_result.scalar_one_or_none.return_value = None
    mock_result.scalar.return_value = 0
    mock_session.execute = AsyncMock(return_value=mock_result)

    score = await engine.compute(agent_id, "overall", mock_session)

    assert 0.45 <= score.score <= 0.55
    assert score.confidence < 0.2
    assert score.interaction_count == 0


@pytest.mark.asyncio
async def test_all_success_interactions_high_score(engine: ScoreComputation) -> None:
    """Many successful interactions → score above 0.7 (counterparty gets full credit)."""
    agent_id = uuid.uuid4()
    counterparty_id = uuid.uuid4()

    # agent_id is the reporter, counterparty_id is the counterparty
    # Counterparty gets full credit (α += w) for success
    interactions = [
        make_interaction(agent_id, counterparty_id, outcome="success") for _ in range(20)
    ]

    engine._fetch_interactions = AsyncMock(return_value=interactions)
    engine._get_cached_score = AsyncMock(return_value=0.5)
    engine._get_auth_trust_level = AsyncMock(return_value="delegated")
    engine._count_lost_disputes = AsyncMock(return_value=0)
    engine._count_dismissed_disputes_filed_by = AsyncMock(return_value=0)
    _setup_sybil_mocks(engine)

    # Compute for counterparty (who gets full credit for successful delivery)
    with patch(_SYBIL_PATCH, return_value=1.0):
        score = await engine.compute(counterparty_id, "overall", AsyncMock())
    assert score.score > 0.7
    assert score.confidence > 0.5


@pytest.mark.asyncio
async def test_all_failure_interactions_low_score(engine: ScoreComputation) -> None:
    """Many failed interactions → score below 0.3 (counterparty gets penalized)."""
    agent_id = uuid.uuid4()
    counterparty_id = uuid.uuid4()

    # agent_id is the reporter, counterparty_id is the counterparty
    # Only counterparty gets penalized (β += w) for failure
    interactions = [
        make_interaction(agent_id, counterparty_id, outcome="failure") for _ in range(20)
    ]

    engine._fetch_interactions = AsyncMock(return_value=interactions)
    engine._get_cached_score = AsyncMock(return_value=0.5)
    engine._get_auth_trust_level = AsyncMock(return_value="delegated")
    engine._count_lost_disputes = AsyncMock(return_value=0)
    engine._count_dismissed_disputes_filed_by = AsyncMock(return_value=0)
    _setup_sybil_mocks(engine)

    # Compute for counterparty (who gets penalized for failures)
    with patch(_SYBIL_PATCH, return_value=1.0):
        score = await engine.compute(counterparty_id, "overall", AsyncMock())
    assert score.score < 0.3


@pytest.mark.asyncio
async def test_root_reporter_shifts_score_more_than_ephemeral(engine: ScoreComputation) -> None:
    """Reports from root agents carry more weight than ephemeral agents."""
    agent_id = uuid.uuid4()
    counterparty_id = uuid.uuid4()

    interactions = [
        make_interaction(agent_id, counterparty_id, outcome="success") for _ in range(5)
    ]

    engine._fetch_interactions = AsyncMock(return_value=interactions)
    engine._get_cached_score = AsyncMock(return_value=0.5)
    engine._count_lost_disputes = AsyncMock(return_value=0)
    engine._count_dismissed_disputes_filed_by = AsyncMock(return_value=0)
    _setup_sybil_mocks(engine)

    with patch(_SYBIL_PATCH, return_value=1.0):
        engine._get_auth_trust_level = AsyncMock(return_value="root")
        score_root = await engine.compute(agent_id, "overall", AsyncMock())

        engine._get_auth_trust_level = AsyncMock(return_value="ephemeral")
        score_ephemeral = await engine.compute(agent_id, "overall", AsyncMock())

    assert score_root.score > score_ephemeral.score


@pytest.mark.asyncio
async def test_mutual_confirmation_bonus(engine: ScoreComputation) -> None:
    """Mutually confirmed interactions carry more weight than one-sided."""
    agent_id = uuid.uuid4()
    counterparty_id = uuid.uuid4()

    one_sided = [
        make_interaction(agent_id, counterparty_id, outcome="success", mutually_confirmed=False)
        for _ in range(5)
    ]
    mutual = [
        make_interaction(agent_id, counterparty_id, outcome="success", mutually_confirmed=True)
        for _ in range(5)
    ]

    engine._get_cached_score = AsyncMock(return_value=0.5)
    engine._get_auth_trust_level = AsyncMock(return_value="delegated")
    engine._count_lost_disputes = AsyncMock(return_value=0)
    engine._count_dismissed_disputes_filed_by = AsyncMock(return_value=0)
    _setup_sybil_mocks(engine)

    with patch(_SYBIL_PATCH, return_value=1.0):
        engine._fetch_interactions = AsyncMock(return_value=one_sided)
        score_one_sided = await engine.compute(agent_id, "overall", AsyncMock())

        engine._fetch_interactions = AsyncMock(return_value=mutual)
        score_mutual = await engine.compute(agent_id, "overall", AsyncMock())

    assert score_mutual.score > score_one_sided.score


@pytest.mark.asyncio
async def test_lost_disputes_apply_penalty(engine: ScoreComputation) -> None:
    """Lost disputes reduce the score with a penalty."""
    agent_id = uuid.uuid4()
    counterparty_id = uuid.uuid4()

    interactions = [
        make_interaction(agent_id, counterparty_id, outcome="success") for _ in range(10)
    ]

    engine._fetch_interactions = AsyncMock(return_value=interactions)
    engine._get_cached_score = AsyncMock(return_value=0.5)
    engine._get_auth_trust_level = AsyncMock(return_value="delegated")
    _setup_sybil_mocks(engine)

    with patch(_SYBIL_PATCH, return_value=1.0):
        engine._count_lost_disputes = AsyncMock(return_value=0)
        engine._count_dismissed_disputes_filed_by = AsyncMock(return_value=0)
        score_clean = await engine.compute(agent_id, "overall", AsyncMock())

        engine._count_lost_disputes = AsyncMock(return_value=5)
        score_penalized = await engine.compute(agent_id, "overall", AsyncMock())

    assert score_penalized.score < score_clean.score


@pytest.mark.asyncio
async def test_score_floor_from_disputes(engine: ScoreComputation) -> None:
    """Dispute penalty is floored at dispute_penalty_floor (0.5)."""
    agent_id = uuid.uuid4()
    counterparty_id = uuid.uuid4()

    interactions = [
        make_interaction(agent_id, counterparty_id, outcome="success") for _ in range(10)
    ]

    engine._fetch_interactions = AsyncMock(return_value=interactions)
    engine._get_cached_score = AsyncMock(return_value=0.5)
    engine._get_auth_trust_level = AsyncMock(return_value="delegated")
    engine._count_lost_disputes = AsyncMock(return_value=1000)
    engine._count_dismissed_disputes_filed_by = AsyncMock(return_value=0)
    _setup_sybil_mocks(engine)

    with patch(_SYBIL_PATCH, return_value=1.0):
        score = await engine.compute(agent_id, "overall", AsyncMock())
    assert score.score >= 0.0
    assert score.factor_breakdown["dispute_penalty"] == engine.dispute_penalty_floor


@pytest.mark.asyncio
async def test_score_always_in_unit_interval(engine: ScoreComputation) -> None:
    """Score is always between 0.0 and 1.0 (test both reporter and counterparty)."""
    agent_id = uuid.uuid4()
    counterparty_id = uuid.uuid4()

    _setup_sybil_mocks(engine)

    with patch(_SYBIL_PATCH, return_value=1.0):
        for outcome in ["success", "failure", "timeout", "partial"]:
            interactions = [
                make_interaction(agent_id, counterparty_id, outcome=outcome) for _ in range(10)
            ]
            engine._fetch_interactions = AsyncMock(return_value=interactions)
            engine._get_cached_score = AsyncMock(return_value=0.5)
            engine._get_auth_trust_level = AsyncMock(return_value="delegated")
            engine._count_lost_disputes = AsyncMock(return_value=0)
            engine._count_dismissed_disputes_filed_by = AsyncMock(return_value=0)

            # Test counterparty (gets full effect of outcomes)
            score = await engine.compute(counterparty_id, "overall", AsyncMock())
            assert 0.0 <= score.score <= 1.0, f"Score out of range for outcome={outcome}"
            assert 0.0 <= score.confidence <= 1.0


@pytest.mark.asyncio
async def test_confidence_increases_with_interactions(engine: ScoreComputation) -> None:
    """More interactions → higher confidence."""
    agent_id = uuid.uuid4()
    counterparty_id = uuid.uuid4()

    engine._get_cached_score = AsyncMock(return_value=0.5)
    engine._get_auth_trust_level = AsyncMock(return_value="delegated")
    engine._count_lost_disputes = AsyncMock(return_value=0)
    engine._count_dismissed_disputes_filed_by = AsyncMock(return_value=0)
    _setup_sybil_mocks(engine)

    confidences = []
    with patch(_SYBIL_PATCH, return_value=1.0):
        for n in [0, 5, 10, 20, 50]:
            interactions = [
                make_interaction(agent_id, counterparty_id, outcome="success") for _ in range(n)
            ]
            engine._fetch_interactions = AsyncMock(return_value=interactions)
            score = await engine.compute(agent_id, "overall", AsyncMock())
            confidences.append(score.confidence)

    for i in range(len(confidences) - 1):
        assert confidences[i] <= confidences[i + 1], f"Confidence not monotone at index {i}"


@pytest.mark.asyncio
async def test_trust_level_weights_ordering(engine: ScoreComputation) -> None:
    """Verify root > delegated > standalone > ephemeral weight ordering."""
    assert TRUST_LEVEL_WEIGHTS["root"] > TRUST_LEVEL_WEIGHTS["delegated"]
    assert TRUST_LEVEL_WEIGHTS["delegated"] > TRUST_LEVEL_WEIGHTS["standalone"]
    assert TRUST_LEVEL_WEIGHTS["standalone"] > TRUST_LEVEL_WEIGHTS["ephemeral"]


@pytest.mark.asyncio
async def test_partial_outcome_intermediate_score(engine: ScoreComputation) -> None:
    """Partial outcomes produce intermediate scores (counterparty scoring)."""
    agent_id = uuid.uuid4()
    counterparty_id = uuid.uuid4()

    engine._get_cached_score = AsyncMock(return_value=0.5)
    engine._get_auth_trust_level = AsyncMock(return_value="delegated")
    engine._count_lost_disputes = AsyncMock(return_value=0)
    engine._count_dismissed_disputes_filed_by = AsyncMock(return_value=0)
    _setup_sybil_mocks(engine)

    n = 20
    # Test counterparty (who gets full effect):
    # success: α += w → high score
    # partial: α += 0.5w, β += 0.5w → neutral
    # failure: β += w → low score
    with patch(_SYBIL_PATCH, return_value=1.0):
        for outcome, expected_range in [
            ("success", (0.65, 1.0)),
            ("partial", (0.45, 0.55)),
            ("failure", (0.0, 0.35)),
        ]:
            interactions = [
                make_interaction(agent_id, counterparty_id, outcome=outcome) for _ in range(n)
            ]
            engine._fetch_interactions = AsyncMock(return_value=interactions)
            score = await engine.compute(counterparty_id, "overall", AsyncMock())
            lo, hi = expected_range
            assert lo <= score.score <= hi, (
                f"outcome={outcome} score={score.score} not in {expected_range}"
            )


# Property-based test with hypothesis
@given(
    n_success=st.integers(min_value=0, max_value=100),
    n_failure=st.integers(min_value=0, max_value=100),
)
@h_settings(max_examples=200)
def test_score_bounded_property(n_success: int, n_failure: int) -> None:
    """Property: score always in [0, 1] for any combination of outcomes (test counterparty)."""
    import asyncio

    async def run() -> None:
        engine = ScoreComputation()
        agent_id = uuid.uuid4()
        counterparty_id = uuid.uuid4()

        # agent_id is reporter, counterparty_id gets full effect
        interactions = [
            make_interaction(agent_id, counterparty_id, outcome="success") for _ in range(n_success)
        ] + [
            make_interaction(agent_id, counterparty_id, outcome="failure") for _ in range(n_failure)
        ]

        engine._fetch_interactions = AsyncMock(return_value=interactions)
        engine._get_cached_score = AsyncMock(return_value=0.5)
        engine._get_auth_trust_level = AsyncMock(return_value="delegated")
        engine._count_lost_disputes = AsyncMock(return_value=0)
        engine._count_dismissed_disputes_filed_by = AsyncMock(return_value=0)
        _setup_sybil_mocks(engine)

        # Test counterparty who experiences full scoring effects
        with patch(_SYBIL_PATCH, return_value=1.0):
            score = await engine.compute(counterparty_id, "overall", AsyncMock())
        assert 0.0 <= score.score <= 1.0
        assert 0.0 <= score.confidence <= 1.0

    asyncio.run(run())


@pytest.mark.asyncio
async def test_dismissed_dispute_penalizes_filer(engine: ScoreComputation) -> None:
    """Filing a frivolous dispute (dismissed) slightly penalizes the filer."""
    agent_id = uuid.uuid4()
    counterparty_id = uuid.uuid4()

    interactions = [
        make_interaction(agent_id, counterparty_id, outcome="success") for _ in range(10)
    ]

    engine._fetch_interactions = AsyncMock(return_value=interactions)
    engine._get_cached_score = AsyncMock(return_value=0.5)
    engine._get_auth_trust_level = AsyncMock(return_value="delegated")
    engine._count_lost_disputes = AsyncMock(return_value=0)
    _setup_sybil_mocks(engine)

    with patch(_SYBIL_PATCH, return_value=1.0):
        engine._count_dismissed_disputes_filed_by = AsyncMock(return_value=0)
        score_clean = await engine.compute(agent_id, "overall", AsyncMock())

        engine._count_dismissed_disputes_filed_by = AsyncMock(return_value=3)
        score_penalized = await engine.compute(agent_id, "overall", AsyncMock())

    assert score_penalized.score < score_clean.score
    assert score_penalized.factor_breakdown.get("dismissed_disputes_filed") == 3


@pytest.mark.asyncio
async def test_dismissed_penalty_floored_at_90_percent(engine: ScoreComputation) -> None:
    """Dismissed dispute penalty is floored at 90%."""
    agent_id = uuid.uuid4()
    counterparty_id = uuid.uuid4()

    interactions = [
        make_interaction(agent_id, counterparty_id, outcome="success") for _ in range(20)
    ]

    engine._fetch_interactions = AsyncMock(return_value=interactions)
    engine._get_cached_score = AsyncMock(return_value=0.5)
    engine._get_auth_trust_level = AsyncMock(return_value="delegated")
    engine._count_lost_disputes = AsyncMock(return_value=0)
    engine._count_dismissed_disputes_filed_by = AsyncMock(return_value=1000)
    _setup_sybil_mocks(engine)

    with patch(_SYBIL_PATCH, return_value=1.0):
        score = await engine.compute(agent_id, "overall", AsyncMock())
    assert score.factor_breakdown["dismissed_penalty"] == 0.90


@pytest.mark.asyncio
async def test_upheld_dispute_factor_breakdown(engine: ScoreComputation) -> None:
    """Upheld disputes appear in factor_breakdown."""
    agent_id = uuid.uuid4()
    counterparty_id = uuid.uuid4()

    interactions = [
        make_interaction(agent_id, counterparty_id, outcome="success") for _ in range(10)
    ]

    engine._fetch_interactions = AsyncMock(return_value=interactions)
    engine._get_cached_score = AsyncMock(return_value=0.5)
    engine._get_auth_trust_level = AsyncMock(return_value="delegated")
    engine._count_lost_disputes = AsyncMock(return_value=2)
    engine._count_dismissed_disputes_filed_by = AsyncMock(return_value=0)
    _setup_sybil_mocks(engine)

    with patch(_SYBIL_PATCH, return_value=1.0):
        score = await engine.compute(agent_id, "overall", AsyncMock())
    assert score.factor_breakdown["lost_disputes"] == 2
    assert score.factor_breakdown["dispute_penalty"] == round(1.0 - 2 * 0.03, 4)
