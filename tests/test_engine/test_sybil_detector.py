from __future__ import annotations

import uuid
from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock

import pytest

from agent_trust.engine.sybil_detector import SybilDetector, SybilReport, SybilSignal


def make_mock_session():
    session = AsyncMock()
    return session


@pytest.mark.asyncio
async def test_check_agent_clean():
    """Agent with no suspicious patterns returns low risk score."""
    session = make_mock_session()
    no_results = MagicMock()
    no_results.fetchall.return_value = []
    no_results.scalar.return_value = 0
    no_results.scalar_one_or_none.return_value = None
    session.execute = AsyncMock(return_value=no_results)

    detector = SybilDetector(session)
    report = await detector.check_agent(str(uuid.uuid4()))

    assert report.risk_score == 0.0
    assert not report.is_suspicious
    assert len(report.signals) == 0


@pytest.mark.asyncio
async def test_invalid_uuid_returns_clean():
    """Invalid UUID agent_id returns zero-risk report without crashing."""
    session = make_mock_session()
    detector = SybilDetector(session)
    report = await detector.check_agent("not-a-uuid")

    assert report.risk_score == 0.0
    assert not report.is_suspicious


@pytest.mark.asyncio
async def test_ring_reporting_detection():
    """Mutual positive reporting triggers ring_reporting signal."""
    session = make_mock_session()
    mutual_agent = uuid.uuid4()

    outgoing_result = MagicMock()
    outgoing_result.fetchall.return_value = [(mutual_agent,), (uuid.uuid4(),), (uuid.uuid4(),)]

    incoming_result = MagicMock()
    incoming_result.scalar.return_value = 3  # 3 mutual reporters

    # cycle check: no rows near agent
    cycle_empty = MagicMock()
    cycle_empty.fetchall.return_value = []

    # burst registration: agent not found (returns None)
    not_found = MagicMock()
    not_found.scalar_one_or_none.return_value = None

    # velocity check: no negative reports
    velocity_zero = MagicMock()
    velocity_zero.scalar.return_value = 0

    session.execute = AsyncMock(
        side_effect=[
            outgoing_result,  # ring: outgoing reports
            incoming_result,  # ring: mutual count
            cycle_empty,      # cycle: rows near agent (empty → returns None)
            not_found,        # burst: agent registered_at (None → returns None)
            not_found,        # delegation: parent (None → returns None)
            velocity_zero,    # velocity: count (0 < threshold → returns None)
        ]
    )

    detector = SybilDetector(session)
    report = await detector.check_agent(str(uuid.uuid4()))

    ring_signals = [s for s in report.signals if s.signal_type == "ring_reporting"]
    assert len(ring_signals) == 1
    assert ring_signals[0].severity > 0.3
    assert ring_signals[0].evidence["mutual_count"] == 3


@pytest.mark.asyncio
async def test_burst_registration_detection():
    """Many agents registered in same window triggers burst_registration signal."""
    session = make_mock_session()
    agent_id = str(uuid.uuid4())
    created_at = datetime.now(UTC)

    no_outgoing = MagicMock()
    no_outgoing.fetchall.return_value = []

    created_result = MagicMock()
    created_result.scalar_one_or_none.return_value = created_at

    burst_result = MagicMock()
    burst_result.scalar.return_value = 12  # 11 others + self

    under_threshold = MagicMock()
    under_threshold.scalar.return_value = 1  # just self → burst_count=0 < threshold

    no_delegation = MagicMock()
    no_delegation.scalar_one_or_none.return_value = None

    cycle_empty = MagicMock()
    cycle_empty.fetchall.return_value = []

    velocity_zero = MagicMock()
    velocity_zero.scalar.return_value = 0

    session.execute = AsyncMock(
        side_effect=[
            no_outgoing,      # ring: outgoing (empty → returns None)
            cycle_empty,      # cycle: rows near agent (empty → returns None)
            created_result,   # burst: agent registered_at
            burst_result,     # burst: count ±1hr (12 → 11 ≥ 5 → triggers)
            under_threshold,  # burst: count ±12hr (1 → 0 < 20 → no trigger)
            under_threshold,  # burst: count ±84hr (1 → 0 < 50 → no trigger)
            no_delegation,    # delegation: parent (None → returns None)
            velocity_zero,    # velocity: count (0 < threshold → returns None)
        ]
    )

    detector = SybilDetector(session)
    report = await detector.check_agent(agent_id)

    burst_signals = [s for s in report.signals if s.signal_type == "burst_registration"]
    assert len(burst_signals) == 1
    assert burst_signals[0].evidence["burst_count"] == 11


@pytest.mark.asyncio
async def test_is_suspicious_threshold():
    """risk_score >= 0.4 makes agent suspicious."""
    report = SybilReport(
        agent_id=str(uuid.uuid4()),
        risk_score=0.45,
        signals=[SybilSignal("ring_reporting", 0.45, "test", {})],
    )
    assert report.is_suspicious
    assert not report.is_high_risk


@pytest.mark.asyncio
async def test_is_high_risk_threshold():
    """risk_score >= 0.7 makes agent high-risk."""
    report = SybilReport(
        agent_id=str(uuid.uuid4()),
        risk_score=0.75,
        signals=[SybilSignal("ring_reporting", 0.75, "test", {})],
    )
    assert report.is_suspicious
    assert report.is_high_risk
