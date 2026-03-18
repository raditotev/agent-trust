from __future__ import annotations

import uuid

import jwt
import structlog

from agent_trust.auth.identity import AgentIdentity, AuthenticationError

log = structlog.get_logger()

# Default scopes for standalone agents
STANDALONE_SCOPES = ["trust.read", "trust.report"]


class StandaloneProvider:
    """Authentication provider for standalone Ed25519 key agents.

    Standalone agents register with a public key and get limited scopes.
    Authentication is either:
    - access_token: a short-lived JWT signed by the agent's Ed25519 private key
      (preferred — cryptographically proves key ownership)
    - public_key_hex: direct key lookup with no crypto proof (legacy/insecure)

    Agents can later upgrade to AgentAuth via link_agentauth.
    """

    def __init__(self, db_session=None) -> None:
        self._db = db_session

    async def authenticate(
        self,
        access_token: str | None = None,
        public_key_hex: str | None = None,
    ) -> AgentIdentity:
        """Authenticate a standalone agent.

        Preferred: pass a signed JWT as access_token (generated via
        scripts/generate_agent_token.py or sign_agent_token()).

        Legacy: pass public_key_hex for a plain key lookup. This does NOT
        verify private key ownership and is only retained for compatibility.
        """
        if access_token:
            return await self._authenticate_token(access_token)
        if public_key_hex:
            return await self._authenticate_public_key_hex(public_key_hex)
        raise AuthenticationError(
            "Standalone provider requires access_token (signed JWT) or public_key_hex"
        )

    async def _authenticate_token(self, token: str) -> AgentIdentity:
        """Verify an agent-signed JWT and return the agent's identity.

        Decodes the token without verification first to extract the agent_id
        from the sub claim, loads the registered public key from the database,
        then performs full signature and expiry verification.
        """
        from agent_trust.crypto.agent_token import public_key_from_bytes, verify_agent_token

        # Peek at sub claim to identify which agent's public key to load
        try:
            unverified = jwt.decode(token, options={"verify_signature": False})
            agent_id_str = unverified.get("sub", "")
            agent_uuid = uuid.UUID(agent_id_str)
        except (jwt.DecodeError, ValueError, AttributeError) as e:
            raise AuthenticationError(f"Invalid standalone token format: {e}") from e

        # Load the agent's registered public key
        agent = await self._lookup_by_agent_id(agent_uuid)
        if not agent:
            raise AuthenticationError(
                f"Agent not found: {agent_id_str}. Register first via register_agent."
            )
        if not agent.public_key:
            raise AuthenticationError(
                f"Agent {agent_id_str} has no registered public key. "
                "Re-register with public_key_hex to use signed token auth."
            )

        # Verify signature against stored public key
        try:
            public_key = public_key_from_bytes(agent.public_key)
            verify_agent_token(token, public_key)
        except jwt.ExpiredSignatureError as e:
            raise AuthenticationError("Standalone token has expired") from e
        except jwt.InvalidTokenError as e:
            raise AuthenticationError(f"Standalone token verification failed: {e}") from e

        log.debug("standalone_token_verified", agent_id=agent_id_str)
        return AgentIdentity(
            agent_id=agent_id_str,
            source="standalone",
            scopes=STANDALONE_SCOPES,
            trust_level="standalone",
        )

    async def _authenticate_public_key_hex(self, public_key_hex: str) -> AgentIdentity:
        """Legacy public key lookup path — REMOVED for security.

        This path had no cryptographic proof of key ownership. Use a signed JWT instead:
        1. Generate a token: call the generate_agent_token tool with your agent_id and
           private_key_hex
        2. Pass the resulting access_token to authenticate
        """
        raise AuthenticationError(
            "Legacy public_key_hex authentication has been removed (no proof of key ownership). "
            "Use a signed JWT instead: call generate_agent_token(agent_id, private_key_hex) "
            "and pass the resulting access_token."
        )

    async def check_permission(
        self,
        identity: AgentIdentity,
        action: str,
        resource: str,
    ) -> bool:
        """Standalone agents have no elevated permissions."""
        return False

    async def _lookup_by_agent_id(self, agent_id: uuid.UUID):
        """Look up an agent by their UUID."""
        if self._db is None:
            return None
        from sqlalchemy import select

        from agent_trust.models import Agent

        result = await self._db.execute(select(Agent).where(Agent.agent_id == agent_id))
        return result.scalar_one_or_none()

    async def _lookup_by_public_key(self, public_key_bytes: bytes):
        """Look up an agent by their Ed25519 public key bytes."""
        if self._db is None:
            return None
        from sqlalchemy import select

        from agent_trust.models import Agent

        result = await self._db.execute(select(Agent).where(Agent.public_key == public_key_bytes))
        return result.scalar_one_or_none()
