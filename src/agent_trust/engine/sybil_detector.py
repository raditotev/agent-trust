from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

import structlog
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

log = structlog.get_logger()


@dataclass
class SybilSignal:
    """A single suspicious activity signal detected for an agent."""

    signal_type: str  # "ring_reporting" | "burst_registration" | "delegation_chain"
    severity: float  # 0.0 (minor) to 1.0 (critical)
    description: str
    evidence: dict = field(default_factory=dict)


@dataclass
class SybilReport:
    """Aggregated sybil detection result for an agent."""

    agent_id: str
    risk_score: float  # 0.0 (clean) to 1.0 (very suspicious)
    signals: list[SybilSignal] = field(default_factory=list)
    checked_at: datetime = field(default_factory=lambda: datetime.now(UTC))

    @property
    def is_suspicious(self) -> bool:
        return self.risk_score >= 0.4

    @property
    def is_high_risk(self) -> bool:
        return self.risk_score >= 0.7


class SybilDetector:
    """Detects suspicious Sybil-attack patterns in agent interactions.

    Three detection strategies:
    1. Ring reporting — mutual high ratings in a closed group
    2. Burst registration — many new agents registered simultaneously
    3. Delegation chain depth — unusually long delegation chains
    """

    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def check_agent(self, agent_id: str) -> SybilReport:
        """Run all sybil detection checks against a single agent.

        Returns a SybilReport with risk_score and individual signals.
        risk_score is the max severity among all signals, capped at 1.0.
        """
        try:
            agent_uuid = uuid.UUID(agent_id)
        except ValueError:
            return SybilReport(agent_id=agent_id, risk_score=0.0)

        signals: list[SybilSignal] = []

        ring = await self._check_ring_reporting(agent_uuid)
        if ring:
            signals.append(ring)

        burst = await self._check_burst_registration(agent_uuid)
        if burst:
            signals.append(burst)

        delegation = await self._check_delegation_chain(agent_uuid)
        if delegation:
            signals.append(delegation)

        risk_score = max((s.severity for s in signals), default=0.0)

        log.info(
            "sybil_check_complete",
            agent_id=agent_id,
            risk_score=round(risk_score, 3),
            signals=[s.signal_type for s in signals],
        )

        return SybilReport(
            agent_id=agent_id,
            risk_score=risk_score,
            signals=signals,
        )

    async def _check_ring_reporting(self, agent_uuid: uuid.UUID) -> SybilSignal | None:
        """Detect ring-reporting: A reports B successfully AND B reports A successfully.

        Looks for mutual positive (success) feedback loops within the last 30 days.
        Severity scales with the number of mutual-report pairs.
        """
        from agent_trust.models.interaction import Interaction

        cutoff = datetime.now(UTC) - timedelta(days=30)

        # Find agents this agent has successfully reported (as reported_by)
        outgoing_result = await self.session.execute(
            select(Interaction.counterparty_id).where(
                Interaction.reported_by == agent_uuid,
                Interaction.outcome == "success",
                Interaction.reported_at >= cutoff,
            )
        )
        reported_agents = {row[0] for row in outgoing_result.fetchall()}

        if not reported_agents:
            return None

        # Find how many of those agents also reported THIS agent successfully
        incoming_result = await self.session.execute(
            select(func.count()).where(
                Interaction.reported_by.in_(reported_agents),
                Interaction.counterparty_id == agent_uuid,
                Interaction.outcome == "success",
                Interaction.reported_at >= cutoff,
            )
        )
        mutual_count = incoming_result.scalar() or 0

        if mutual_count < 2:
            return None

        severity = min(0.3 + (mutual_count - 2) * 0.1, 0.9)
        return SybilSignal(
            signal_type="ring_reporting",
            severity=severity,
            description=f"{mutual_count} mutual positive reports detected in last 30 days",
            evidence={"mutual_count": mutual_count, "reported_agents": len(reported_agents)},
        )

    async def _check_burst_registration(self, agent_uuid: uuid.UUID) -> SybilSignal | None:
        """Detect burst registration: many agents registered within a short time window.

        Checks if many agents were registered within ±1 hour of this agent's creation.
        Severity scales with the burst size.
        """
        from agent_trust.models.agent import Agent

        result = await self.session.execute(
            select(Agent.registered_at).where(Agent.agent_id == agent_uuid)
        )
        registered_at = result.scalar_one_or_none()
        if not registered_at:
            return None

        window_start = registered_at - timedelta(hours=1)
        window_end = registered_at + timedelta(hours=1)

        burst_count_result = await self.session.execute(
            select(func.count())
            .select_from(Agent)
            .where(
                Agent.registered_at >= window_start,
                Agent.registered_at <= window_end,
            )
        )
        burst_count = (burst_count_result.scalar() or 1) - 1  # exclude self

        if burst_count < 5:
            return None

        severity = min(0.2 + burst_count * 0.04, 0.8)
        return SybilSignal(
            signal_type="burst_registration",
            severity=severity,
            description=f"{burst_count} agents registered within 1 hour of this agent",
            evidence={"burst_count": burst_count, "window_hours": 2},
        )

    async def _check_delegation_chain(self, agent_uuid: uuid.UUID) -> SybilSignal | None:
        """Detect abnormally long delegation chains.

        Uses the Agent.delegated_by field to walk the delegation chain upward.
        Chains longer than 3 hops are suspicious; 5+ are high-risk.
        """
        from agent_trust.models.agent import Agent

        chain_length = 0
        current_id = agent_uuid
        visited: set[uuid.UUID] = {current_id}

        for _ in range(10):  # safety cap at 10 hops
            result = await self.session.execute(
                select(Agent.delegated_by).where(Agent.agent_id == current_id)
            )
            parent_id = result.scalar_one_or_none()
            if not parent_id or parent_id in visited:
                break
            chain_length += 1
            current_id = parent_id
            visited.add(current_id)

        if chain_length < 3:
            return None

        severity = min(0.3 + (chain_length - 3) * 0.15, 0.85)
        return SybilSignal(
            signal_type="delegation_chain",
            severity=severity,
            description=f"Delegation chain depth {chain_length} (threshold: 3)",
            evidence={"chain_depth": chain_length},
        )


async def get_sybil_credibility_multiplier(
    agent_id: uuid.UUID,
    session: AsyncSession,
) -> float:
    """Return a credibility multiplier [0.0, 1.0] for Sybil-flagged agents.

    Returns 1.0 (full credibility) for clean agents.
    Returns < 1.0 for agents with detected suspicious patterns.
    """
    detector = SybilDetector(session)
    report = await detector.check_agent(str(agent_id))
    if report.is_high_risk:
        return 0.3
    if report.is_suspicious:
        return 0.6
    return 1.0
