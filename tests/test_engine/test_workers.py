from __future__ import annotations

import uuid
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from agent_trust.workers.attestation_expiry import expire_attestations
from agent_trust.workers.score_recomputer import recompute_score
from tests.factories import make_trust_score


@pytest.mark.asyncio
async def test_recompute_score_success():
    """recompute_score updates scores and invalidates cache."""
    agent_id = str(uuid.uuid4())
    mock_score = make_trust_score(uuid.UUID(agent_id), score=0.75)

    mock_session = AsyncMock()
    mock_ctx = AsyncMock()
    mock_ctx.__aenter__ = AsyncMock(return_value=mock_session)
    mock_ctx.__aexit__ = AsyncMock(return_value=False)

    mock_engine = AsyncMock()
    mock_engine.compute = AsyncMock(return_value=mock_score)

    mock_redis = AsyncMock()
    mock_redis.delete = AsyncMock()

    with (
        patch("agent_trust.workers.score_recomputer.get_session", return_value=mock_ctx),
        patch("agent_trust.workers.score_recomputer.ScoreComputation", return_value=mock_engine),
        patch("agent_trust.workers.score_recomputer.upsert_trust_score", new=AsyncMock()),
        patch(
            "agent_trust.workers.score_recomputer.get_redis",
            return_value=AsyncMock(return_value=mock_redis),
        ),
    ):
        result = await recompute_score({}, agent_id)

    assert result["agent_id"] == agent_id
    assert "updated_scores" in result


@pytest.mark.asyncio
async def test_recompute_score_invalid_uuid():
    """recompute_score returns error for invalid UUID."""
    result = await recompute_score({}, "not-a-uuid")
    assert "error" in result


@pytest.mark.asyncio
async def test_expire_attestations_marks_expired():
    """expire_attestations marks past-due attestations as revoked."""
    from datetime import UTC, datetime, timedelta

    from agent_trust.models import Attestation

    expired_attestation = MagicMock(spec=Attestation)
    expired_attestation.revoked = False
    expired_attestation.valid_until = datetime.now(UTC) - timedelta(hours=1)

    mock_session = AsyncMock()
    result_mock = MagicMock()
    result_mock.scalars.return_value.all.return_value = [expired_attestation]
    mock_session.execute = AsyncMock(return_value=result_mock)

    mock_ctx = AsyncMock()
    mock_ctx.__aenter__ = AsyncMock(return_value=mock_session)
    mock_ctx.__aexit__ = AsyncMock(return_value=False)

    with patch("agent_trust.workers.attestation_expiry.get_session", return_value=mock_ctx):
        result = await expire_attestations({})

    assert result["revoked"] == 1
    assert expired_attestation.revoked is True
