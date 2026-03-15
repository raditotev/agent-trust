from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class AgentIdentity:
    """Resolved identity for an authenticated agent."""
    agent_id: str
    source: str  # "agentauth" | "standalone"
    scopes: list[str] = field(default_factory=list)
    trust_level: str = "ephemeral"  # "root" | "delegated" | "ephemeral" | "standalone"

    def has_scope(self, scope: str) -> bool:
        """Check if this identity has the given scope."""
        return scope in self.scopes

    def has_any_scope(self, *scopes: str) -> bool:
        """Check if this identity has any of the given scopes."""
        return any(s in self.scopes for s in scopes)


class AuthenticationError(Exception):
    """Raised when agent authentication fails."""


class AuthorizationError(Exception):
    """Raised when agent lacks required permission."""
