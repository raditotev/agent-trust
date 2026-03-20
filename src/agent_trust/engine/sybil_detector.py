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

    signal_type: str  # "ring_reporting" | "burst_registration" | "burst_registration_medium"
    #                    | "burst_registration_slow" | "reporting_velocity" | "delegation_chain"
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
        cycle = await self._check_cycle_reporting(agent_uuid)
        for sig in (ring, cycle):
            if sig:
                signals.append(sig)

        burst = await self._check_burst_registration(agent_uuid)
        if burst:
            signals.append(burst)

        delegation = await self._check_delegation_chain(agent_uuid)
        if delegation:
            signals.append(delegation)

        velocity = await self._check_reporting_velocity(agent_uuid)
        if velocity:
            signals.append(velocity)

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
        """Detect burst registration across short and long time windows.

        Checks three windows to catch both fast bursts and slow Sybil armies:
        - ±1 hour: threshold 5 (fast burst)
        - ±12 hours: threshold 20 (medium-rate Sybil)
        - ±84 hours (3.5 days): threshold 50 (slow Sybil army)
        Severity scales with window and burst size. Returns the most severe signal.
        """
        from agent_trust.config import settings
        from agent_trust.models.agent import Agent

        result = await self.session.execute(
            select(Agent.registered_at).where(Agent.agent_id == agent_uuid)
        )
        registered_at = result.scalar_one_or_none()
        if not registered_at:
            return None

        windows = [
            (1, 5, "burst_registration"),  # ±1hr, ≥5
            (12, settings.sybil_burst_24h_threshold, "burst_registration_medium"),  # ±12hr, ≥20
            (84, settings.sybil_burst_7d_threshold, "burst_registration_slow"),  # ±84hr, ≥50
        ]

        best_signal: SybilSignal | None = None
        for window_hours, threshold, signal_type in windows:
            window_start = registered_at - timedelta(hours=window_hours)
            window_end = registered_at + timedelta(hours=window_hours)
            count_result = await self.session.execute(
                select(func.count())
                .select_from(Agent)
                .where(
                    Agent.registered_at >= window_start,
                    Agent.registered_at <= window_end,
                )
            )
            burst_count = (count_result.scalar() or 1) - 1  # exclude self
            if burst_count >= threshold:
                severity = min(0.2 + burst_count * 0.04, 0.8)
                signal = SybilSignal(
                    signal_type=signal_type,
                    severity=severity,
                    description=(
                        f"{burst_count} agents registered within ±{window_hours}h "
                        f"of this agent (threshold: {threshold})"
                    ),
                    evidence={
                        "burst_count": burst_count,
                        "window_hours": window_hours * 2,
                        "threshold": threshold,
                    },
                )
                if best_signal is None or signal.severity > best_signal.severity:
                    best_signal = signal

        return best_signal

    async def _check_cycle_reporting(self, agent_uuid: uuid.UUID) -> SybilSignal | None:
        """Detect multi-hop positive-report cycles: A→B→C→D→A (length 3-6).

        The ring_reporting check only finds 2-agent mutual pairs.
        This method uses BFS to detect longer cycles in the success-report graph
        within the last 30 days. Cycles of length 3-6 are suspicious.

        BFS is bounded to MAX_VISITED nodes to prevent OOM on large graphs.
        If the limit is reached, a partial_scan flag is included in the evidence.
        """
        from collections import deque

        from agent_trust.models.interaction import Interaction

        cutoff = datetime.now(UTC) - timedelta(days=30)
        MAX_HOPS = 6
        MAX_VISITED = 500  # cap BFS to prevent OOM on large connected components

        # Build outgoing success-report adjacency within 1-hop neighbourhood
        rows_result = await self.session.execute(
            select(Interaction.reported_by, Interaction.counterparty_id).where(
                Interaction.outcome == "success",
                Interaction.reported_at >= cutoff,
                (Interaction.reported_by == agent_uuid)
                | (Interaction.counterparty_id == agent_uuid),
            )
        )
        rows = rows_result.fetchall()
        if not rows:
            return None

        neighbours: set[uuid.UUID] = set()
        for reporter, counterparty in rows:
            neighbours.add(reporter)
            neighbours.add(counterparty)

        if len(neighbours) < 3:
            return None

        # Fetch all success edges among the neighbourhood
        edges_result = await self.session.execute(
            select(Interaction.reported_by, Interaction.counterparty_id).where(
                Interaction.outcome == "success",
                Interaction.reported_at >= cutoff,
                Interaction.reported_by.in_(neighbours),
                Interaction.counterparty_id.in_(neighbours),
            )
        )
        graph: dict[uuid.UUID, set[uuid.UUID]] = {}
        for reporter, counterparty in edges_result.fetchall():
            graph.setdefault(reporter, set()).add(counterparty)

        if agent_uuid not in graph:
            return None

        # BFS from agent_uuid to find shortest cycle back to itself
        queue: deque[tuple[uuid.UUID, list[uuid.UUID]]] = deque()
        for neighbour in graph.get(agent_uuid, set()):
            if neighbour != agent_uuid:
                queue.append((neighbour, [agent_uuid, neighbour]))

        shortest_cycle: list[uuid.UUID] | None = None
        visited_count = 0
        partial_scan = False
        while queue:
            current, path = queue.popleft()
            visited_count += 1
            if visited_count > MAX_VISITED:
                partial_scan = True
                log.warning(
                    "cycle_bfs_truncated",
                    agent_id=str(agent_uuid),
                    visited=visited_count,
                    max_visited=MAX_VISITED,
                )
                break
            if len(path) > MAX_HOPS + 1:
                continue
            if current == agent_uuid and len(path) >= 3:
                shortest_cycle = path
                break
            for nxt in graph.get(current, set()):
                if nxt not in path[1:] or nxt == agent_uuid:
                    queue.append((nxt, path + [nxt]))

        if not shortest_cycle and not partial_scan:
            return None

        if not shortest_cycle and partial_scan:
            # Couldn't complete scan — return low-severity advisory signal
            return SybilSignal(
                signal_type="ring_reporting",
                severity=0.25,
                description=(
                    f"Cycle detection scan truncated after {MAX_VISITED} nodes — "
                    "agent is in a large connected component of mutual reporters"
                ),
                evidence={
                    "partial_scan": True,
                    "visited_nodes": visited_count,
                    "max_visited": MAX_VISITED,
                    "cycle_node_count": len(neighbours),
                },
            )

        cycle_length = len(shortest_cycle) - 1  # edges in cycle
        severity = min(0.4 + (cycle_length - 2) * 0.1, 0.85)
        return SybilSignal(
            signal_type="ring_reporting",
            severity=severity,
            description=(
                f"Positive-report cycle of length {cycle_length} detected "
                f"in last 30 days (multi-hop ring)"
            ),
            evidence={
                "cycle_length": cycle_length,
                "cycle_node_count": len(neighbours),
                "partial_scan": partial_scan,
            },
        )

    async def _check_reporting_velocity(self, agent_uuid: uuid.UUID) -> SybilSignal | None:
        """Detect abnormally high negative-report velocity.

        A legitimate agent rarely reports >50 distinct counterparties as failure/timeout
        within a 24-hour window. High velocity suggests a trust bomb attack.
        """
        from agent_trust.config import settings
        from agent_trust.models.interaction import Interaction

        cutoff = datetime.now(UTC) - timedelta(hours=24)

        velocity_result = await self.session.execute(
            select(func.count(Interaction.counterparty_id.distinct())).where(
                Interaction.reported_by == agent_uuid,
                Interaction.outcome.in_(["failure", "timeout"]),
                Interaction.reported_at >= cutoff,
            )
        )
        distinct_negative_count = velocity_result.scalar() or 0

        threshold = settings.sybil_report_velocity_threshold
        if distinct_negative_count < threshold:
            return None

        severity = min(0.5 + (distinct_negative_count - threshold) * 0.01, 0.95)
        return SybilSignal(
            signal_type="reporting_velocity",
            severity=severity,
            description=(
                f"Agent filed {distinct_negative_count} negative reports against "
                f"distinct counterparties in the last 24h (threshold: {threshold})"
            ),
            evidence={
                "distinct_negative_reports_24h": distinct_negative_count,
                "threshold": threshold,
            },
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
