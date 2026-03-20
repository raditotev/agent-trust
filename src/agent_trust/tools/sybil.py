from __future__ import annotations

import structlog

from agent_trust.db.session import get_session
from agent_trust.engine.sybil_detector import SybilDetector

log = structlog.get_logger()


async def sybil_check(agent_id: str) -> dict:
    """Run sybil detection checks against an agent.

    Detects three suspicious patterns:
    - ring_reporting: mutual positive feedback loops (A rates B high, B rates A high)
    - burst_registration: many agents registered in a short time window
    - delegation_chain: unusually long delegation chains (>3 hops)

    Returns risk_score (0.0 clean → 1.0 suspicious), is_suspicious flag,
    and detailed signals with severity and evidence.

    No authentication required — this is a public safety tool.

    Example call:
        sybil_check(agent_id="550e8400-e29b-41d4-a716-446655440000")

    Example response:
        {
            "agent_id": "550e8400-...",
            "risk_score": 0.0,
            "is_suspicious": false,
            "is_high_risk": false,
            "signals": [],
            "checked_at": "2026-03-20T12:00:00+00:00"
        }
    """
    async with get_session() as session:
        detector = SybilDetector(session)
        report = await detector.check_agent(agent_id)

    return {
        "agent_id": report.agent_id,
        "risk_score": round(report.risk_score, 4),
        "is_suspicious": report.is_suspicious,
        "is_high_risk": report.is_high_risk,
        "signals": [
            {
                "signal_type": s.signal_type,
                "severity": round(s.severity, 4),
                "description": s.description,
                "evidence": s.evidence,
            }
            for s in report.signals
        ],
        "checked_at": report.checked_at.isoformat(),
    }
