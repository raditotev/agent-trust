from __future__ import annotations

from typing import Protocol, runtime_checkable

from agent_trust.auth.identity import AgentIdentity


@runtime_checkable
class AuthProvider(Protocol):
    """Protocol for authentication providers."""

    async def authenticate(
        self,
        access_token: str | None = None,
        public_key_hex: str | None = None,
    ) -> AgentIdentity:
        """Authenticate an agent and return their identity.

        Raises AuthenticationError if authentication fails.
        """
        ...

    async def check_permission(
        self,
        identity: AgentIdentity,
        action: str,
        resource: str,
    ) -> bool:
        """Check if the agent has permission to perform action on resource."""
        ...


def require_scope(identity: AgentIdentity, scope: str) -> None:
    """Raise AuthorizationError if the identity lacks the required scope."""
    from agent_trust.auth.identity import AuthorizationError
    if not identity.has_scope(scope):
        raise AuthorizationError(
            f"Required scope '{scope}' not present. Agent has: {identity.scopes}"
        )
