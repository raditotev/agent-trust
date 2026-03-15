from __future__ import annotations

import uuid
from datetime import UTC, datetime

from agent_trust.models import Agent, Dispute, Interaction, TrustScore


def make_agent(
    agent_id: uuid.UUID | None = None,
    display_name: str | None = None,
    auth_source: str = "agentauth",
    capabilities: list[str] | None = None,
    trust_level: float = 0.5,
    agentauth_linked: bool = True,
    public_key: bytes | None = None,
    status: str = "active",
) -> Agent:
    """Create an Agent ORM instance for testing (not persisted)."""
    return Agent(
        agent_id=agent_id or uuid.uuid4(),
        display_name=display_name or f"Test Agent {uuid.uuid4().hex[:8]}",
        capabilities=capabilities or [],
        metadata_={},
        trust_level=trust_level,
        auth_source=auth_source,
        public_key=public_key,
        agentauth_linked=agentauth_linked,
        registered_at=datetime.now(UTC),
        status=status,
    )


def make_standalone_agent(
    agent_id: uuid.UUID | None = None,
    public_key_hex: str = "deadbeef01020304",
) -> Agent:
    """Create a standalone agent with an Ed25519 public key."""
    return make_agent(
        agent_id=agent_id,
        auth_source="standalone",
        agentauth_linked=False,
        public_key=bytes.fromhex(public_key_hex),
    )


def make_interaction(
    initiator_id: uuid.UUID,
    counterparty_id: uuid.UUID,
    outcome: str = "success",
    interaction_type: str = "transaction",
    reported_by: uuid.UUID | None = None,
    mutually_confirmed: bool = False,
) -> Interaction:
    """Create an Interaction ORM instance for testing (not persisted)."""
    return Interaction(
        interaction_id=uuid.uuid4(),
        initiator_id=initiator_id,
        counterparty_id=counterparty_id,
        interaction_type=interaction_type,
        outcome=outcome,
        context={},
        reported_by=reported_by or initiator_id,
        mutually_confirmed=mutually_confirmed,
        reported_at=datetime.now(UTC),
    )


def make_trust_score(
    agent_id: uuid.UUID,
    score_type: str = "overall",
    score: float = 0.75,
    confidence: float = 0.80,
    interaction_count: int = 10,
) -> TrustScore:
    """Create a TrustScore ORM instance for testing (not persisted)."""
    return TrustScore(
        agent_id=agent_id,
        score_type=score_type,
        score=score,
        confidence=confidence,
        interaction_count=interaction_count,
        factor_breakdown={
            "bayesian_raw": score,
            "dispute_penalty": 1.0,
            "interactions_weighted": interaction_count,
            "lost_disputes": 0,
        },
        computed_at=datetime.now(UTC),
    )


def make_dispute(
    interaction_id: uuid.UUID,
    filed_by: uuid.UUID,
    filed_against: uuid.UUID,
    status: str = "open",
    resolution: str | None = None,
) -> Dispute:
    """Create a Dispute ORM instance for testing (not persisted)."""
    return Dispute(
        dispute_id=uuid.uuid4(),
        interaction_id=interaction_id,
        filed_by=filed_by,
        filed_against=filed_against,
        reason="Test dispute reason",
        evidence={},
        status=status,
        resolution=resolution,
        created_at=datetime.now(UTC),
    )
