from __future__ import annotations

from agent_trust.auth.identity import AgentIdentity


def test_agent_identity_has_scope():
    identity = AgentIdentity(
        agent_id="test",
        source="agentauth",
        scopes=["trust.read", "trust.report"],
    )
    assert identity.has_scope("trust.read") is True
    assert identity.has_scope("trust.admin") is False


def test_agent_identity_has_any_scope():
    identity = AgentIdentity(
        agent_id="test",
        source="agentauth",
        scopes=["trust.read"],
    )
    assert identity.has_any_scope("trust.read", "trust.admin") is True
    assert identity.has_any_scope("trust.admin", "trust.dispute.resolve") is False
