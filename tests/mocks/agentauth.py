from __future__ import annotations

import time
import uuid


def make_token_introspection(
    agent_id: str | None = None,
    active: bool = True,
    scopes: list[str] | None = None,
    trust_level: str = "delegated",
    exp_offset: int = 3600,
) -> dict:
    """Create a mock AgentAuth token introspection response."""
    if agent_id is None:
        agent_id = str(uuid.uuid4())
    if scopes is None:
        scopes = ["trust.read", "trust.report"]

    return {
        "active": active,
        "sub": agent_id,
        "agent_id": agent_id,
        "scopes": scopes,
        "scope": " ".join(scopes),
        "trust_level": trust_level,
        "agent_type": "tool",
        "exp": int(time.time()) + exp_offset,
        "iat": int(time.time()),
    }


def make_permission_check(allowed: bool = True) -> dict:
    """Create a mock AgentAuth permission check response."""
    return {"allowed": allowed, "reason": "mock"}


MOCK_ROOT_AGENT = make_token_introspection(
    trust_level="root",
    scopes=["trust.read", "trust.report", "trust.dispute.file", "trust.dispute.resolve", "trust.attest.issue", "trust.admin"],
)

MOCK_DELEGATED_AGENT = make_token_introspection(
    trust_level="delegated",
    scopes=["trust.read", "trust.report", "trust.dispute.file"],
)

MOCK_EPHEMERAL_AGENT = make_token_introspection(
    trust_level="ephemeral",
    scopes=["trust.read"],
)
