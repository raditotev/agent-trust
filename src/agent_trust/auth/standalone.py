from __future__ import annotations

import binascii

import structlog

from agent_trust.auth.identity import AgentIdentity, AuthenticationError

log = structlog.get_logger()

# Default scopes for standalone agents
STANDALONE_SCOPES = ["trust.read", "trust.report"]


class StandaloneProvider:
    """Authentication provider for standalone Ed25519 key agents.

    Standalone agents register with a public key and get limited scopes.
    They can later upgrade via link_agentauth.
    """

    def __init__(self, db_session=None) -> None:
        self._db = db_session

    async def authenticate(
        self,
        access_token: str | None = None,
        public_key_hex: str | None = None,
    ) -> AgentIdentity:
        """Authenticate via Ed25519 public key lookup."""
        if not public_key_hex:
            raise AuthenticationError("Standalone provider requires public_key_hex")

        try:
            public_key_bytes = bytes.fromhex(public_key_hex)
        except (ValueError, binascii.Error) as e:
            raise AuthenticationError(f"Invalid public_key_hex format: {e}") from e

        agent = await self._lookup_by_public_key(public_key_bytes)
        if not agent:
            raise AuthenticationError(
                "Unknown public key — register first via register_agent"
            )

        return AgentIdentity(
            agent_id=str(agent.agent_id),
            source="standalone",
            scopes=STANDALONE_SCOPES,
            trust_level="standalone",
        )

    async def check_permission(
        self,
        identity: AgentIdentity,
        action: str,
        resource: str,
    ) -> bool:
        """Standalone agents have no elevated permissions."""
        return False

    async def _lookup_by_public_key(self, public_key_bytes: bytes):
        """Look up an agent by their Ed25519 public key."""
        if self._db is None:
            return None
        from sqlalchemy import select
        from agent_trust.models import Agent
        result = await self._db.execute(
            select(Agent).where(Agent.public_key == public_key_bytes)
        )
        return result.scalar_one_or_none()
