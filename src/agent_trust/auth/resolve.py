from __future__ import annotations

import structlog

from agent_trust.auth.identity import AgentIdentity, AuthenticationError
from agent_trust.config import settings

log = structlog.get_logger()


async def resolve_identity(
    access_token: str | None = None,
    public_key_hex: str | None = None,
) -> AgentIdentity:
    """Resolve agent identity from an access token or standalone public key.

    Resolution order:
    1. If access_token looks like a self-issued standalone JWT (iss == sub == UUID,
       aud == "agent-trust") → verify with StandaloneProvider (cryptographic proof).
    2. If access_token is provided and auth_provider includes AgentAuth → introspect
       via AgentAuth MCP server.
    3. If public_key_hex is provided and auth_provider includes standalone → do a
       plain key lookup (legacy path, no crypto proof of key ownership).

    Raises AuthenticationError if no path succeeds.
    """
    from agent_trust.auth.agentauth import AgentAuthProvider
    from agent_trust.auth.standalone import StandaloneProvider
    from agent_trust.crypto.agent_token import is_standalone_agent_token
    from agent_trust.db.redis import get_redis
    from agent_trust.db.session import get_session

    if access_token:
        # Detect self-issued standalone agent JWT before hitting AgentAuth
        if is_standalone_agent_token(access_token) and settings.auth_provider in (
            "standalone",
            "both",
        ):
            async with get_session() as session:
                provider = StandaloneProvider(db_session=session)
                return await provider.authenticate(access_token=access_token)

        # Fall through to AgentAuth for opaque tokens or AgentAuth JWTs
        if settings.auth_provider in ("agentauth", "both"):
            redis = await get_redis()
            provider = AgentAuthProvider(redis_client=redis)
            identity = await provider.authenticate(access_token=access_token)

            # Reverse lookup: if this AgentAuth UUID maps to a linked standalone profile
            # (i.e. link_agentauth was called and stored agentauth_id in metadata_),
            # return the standalone agent's canonical ID so all profile lookups work.
            from sqlalchemy import select

            from agent_trust.db.session import get_session
            from agent_trust.models import Agent

            async with get_session() as session:
                result = await session.execute(
                    select(Agent).where(
                        Agent.metadata_["agentauth_id"].astext == identity.agent_id
                    )
                )
                linked = result.scalar_one_or_none()
                if linked:
                    return AgentIdentity(
                        agent_id=str(linked.agent_id),
                        source="agentauth",
                        scopes=identity.scopes,
                        trust_level=identity.trust_level,
                    )

            return identity

    # Legacy: plain public key lookup (no proof of private key ownership)
    if public_key_hex and settings.auth_provider in ("standalone", "both"):
        async with get_session() as session:
            provider = StandaloneProvider(db_session=session)
            return await provider.authenticate(public_key_hex=public_key_hex)

    raise AuthenticationError(
        "Authentication required. Provide access_token (AgentAuth token or standalone "
        "signed JWT) or public_key_hex (standalone legacy). "
        f"Current auth_provider setting: {settings.auth_provider}"
    )
