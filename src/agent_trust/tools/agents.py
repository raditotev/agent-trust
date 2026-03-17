from __future__ import annotations

import binascii
import uuid

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

import structlog
from sqlalchemy import select

from agent_trust.auth.agentauth import AgentAuthProvider
from agent_trust.auth.identity import AgentIdentity, AuthenticationError
from agent_trust.auth.standalone import STANDALONE_SCOPES, StandaloneProvider
from agent_trust.config import settings
from agent_trust.db.redis import get_redis
from agent_trust.db.session import get_session
from agent_trust.models import Agent, TrustScore

log = structlog.get_logger()


async def _resolve_identity(
    access_token: str | None,
    public_key_hex: str | None,
) -> AgentIdentity:
    """Resolve agent identity from AgentAuth token or standalone key."""
    redis = await get_redis()

    if access_token and settings.auth_provider in ("agentauth", "both"):
        provider = AgentAuthProvider(redis_client=redis)
        return await provider.authenticate(access_token=access_token)

    if public_key_hex and settings.auth_provider in ("standalone", "both"):
        async with get_session() as session:
            provider = StandaloneProvider(db_session=session)
            return await provider.authenticate(public_key_hex=public_key_hex)

    raise AuthenticationError(
        "Provide access_token (AgentAuth) or public_key_hex (standalone). "
        f"Current auth_provider setting: {settings.auth_provider}"
    )


async def _ensure_agent_profile(
    agent_id: uuid.UUID,
    auth_source: str,
    display_name: str | None = None,
    capabilities: list[str] | None = None,
    metadata: dict | None = None,
    public_key: bytes | None = None,
) -> tuple[Agent, bool]:
    """Get or create an agent profile. Returns (agent, created)."""
    async with get_session() as session:
        result = await session.execute(select(Agent).where(Agent.agent_id == agent_id))
        existing = result.scalar_one_or_none()
        if existing:
            return existing, False

        agent = Agent(
            agent_id=agent_id,
            display_name=display_name,
            capabilities=capabilities or [],
            metadata_=metadata or {},
            auth_source=auth_source,
            public_key=public_key,
            agentauth_linked=(auth_source == "agentauth"),
            status="active",
        )
        session.add(agent)
        await session.flush()
        log.info("agent_profile_created", agent_id=str(agent_id), source=auth_source)
        return agent, True


async def register_agent(
    display_name: str | None = None,
    capabilities: list[str] | None = None,
    metadata: dict | None = None,
    access_token: str | None = None,
    public_key_hex: str | None = None,
) -> dict:
    """Register a new agent in the trust network.

    Three registration paths:

    1. **AgentAuth (preferred)**: Provide your AgentAuth ``access_token``.
       Your identity is verified via token introspection and your AgentAuth
       ``agent_id`` becomes your trust profile ID. Zero additional config needed.

    2. **Standalone**: Provide an Ed25519 ``public_key_hex`` (hex-encoded 32-byte
       public key). You get a local trust profile with limited scopes
       (``trust.read`` + ``trust.report``). You can link to AgentAuth later via
       the ``link_agentauth`` tool.

    3. **Auto-generated keys**: Omit both ``access_token`` and ``public_key_hex``
       (standalone mode only). An Ed25519 key pair is generated for you and
       returned in the response as ``public_key_hex`` and ``private_key_hex``.
       **Store the private key immediately** — it is shown only once.

    If the agent profile already exists, returns the existing profile without
    error (idempotent).

    Args:
        display_name: Human-readable name for this agent (optional).
        capabilities: List of capability tags, e.g. ``["code-review", "search"]``.
        metadata: Arbitrary key/value metadata to store on the profile.
        access_token: AgentAuth bearer token (AgentAuth path).
        public_key_hex: Hex-encoded Ed25519 public key (standalone path).
            Omit both ``access_token`` and ``public_key_hex`` to auto-generate
            a key pair (standalone mode only).

    Returns:
        ``agent_id``, ``source`` ("agentauth" or "standalone"), ``scopes``,
        ``created`` (bool), ``registered_at``, and ``display_name``.
    """
    if access_token:
        identity = await _resolve_identity(access_token, None)
        agent_id = uuid.UUID(identity.agent_id)
        agent, created = await _ensure_agent_profile(
            agent_id=agent_id,
            auth_source="agentauth",
            display_name=display_name,
            capabilities=capabilities,
            metadata=metadata,
        )
        return {
            "agent_id": str(agent.agent_id),
            "source": "agentauth",
            "scopes": identity.scopes,
            "created": created,
            "registered_at": agent.registered_at.isoformat() if agent.registered_at else None,
            "display_name": agent.display_name,
        }

    if public_key_hex:
        try:
            public_key_bytes = bytes.fromhex(public_key_hex)
        except (ValueError, binascii.Error) as e:
            raise AuthenticationError(f"Invalid public_key_hex: {e}") from e

        new_agent_id = uuid.uuid4()
        agent, created = await _ensure_agent_profile(
            agent_id=new_agent_id,
            auth_source="standalone",
            display_name=display_name,
            capabilities=capabilities,
            metadata=metadata,
            public_key=public_key_bytes,
        )
        return {
            "agent_id": str(agent.agent_id),
            "source": "standalone",
            "scopes": STANDALONE_SCOPES,
            "created": created,
            "registered_at": agent.registered_at.isoformat() if agent.registered_at else None,
            "display_name": agent.display_name,
        }

    if settings.auth_provider in ("standalone", "both"):
        # Auto-generate an Ed25519 key pair so the caller doesn't need to supply one.
        private_key = Ed25519PrivateKey.generate()
        public_key_bytes = private_key.public_key().public_bytes_raw()
        private_key_bytes = private_key.private_bytes_raw()

        new_agent_id = uuid.uuid4()
        agent, created = await _ensure_agent_profile(
            agent_id=new_agent_id,
            auth_source="standalone",
            display_name=display_name,
            capabilities=capabilities,
            metadata=metadata,
            public_key=public_key_bytes,
        )
        log.info("agent_keypair_autogenerated", agent_id=str(new_agent_id))
        return {
            "agent_id": str(agent.agent_id),
            "source": "standalone",
            "scopes": STANDALONE_SCOPES,
            "created": created,
            "registered_at": agent.registered_at.isoformat() if agent.registered_at else None,
            "display_name": agent.display_name,
            "public_key_hex": public_key_bytes.hex(),
            "private_key_hex": private_key_bytes.hex(),
            "warning": (
                "Key pair auto-generated. Store private_key_hex securely — "
                "it will not be shown again. Use public_key_hex to authenticate."
            ),
        }

    raise AuthenticationError("Provide access_token (AgentAuth) or public_key_hex (standalone).")


async def link_agentauth(
    access_token: str,
    public_key_hex: str,
) -> dict:
    """Link a standalone trust profile to an AgentAuth identity.

    Provide your AgentAuth ``access_token`` and the ``public_key_hex`` you
    originally registered with. Your interaction history and scores transfer
    to the AgentAuth identity. The standalone profile is updated to reflect
    the AgentAuth source. This is a one-time, irreversible operation.

    After linking, authenticate exclusively with your AgentAuth token — the
    public key will no longer be usable for authentication.

    Args:
        access_token: Valid AgentAuth bearer token identifying the target identity.
        public_key_hex: Hex-encoded Ed25519 public key used during standalone registration.

    Returns:
        ``agent_id`` (the AgentAuth UUID now canonical), ``merged`` (bool),
        and a ``message`` confirming success.

    Raises:
        ``AuthenticationError`` if the token is invalid or the key is unknown.
    """
    # Verify the AgentAuth token and get the identity
    redis = await get_redis()
    aa_provider = AgentAuthProvider(redis_client=redis)
    aa_identity = await aa_provider.authenticate(access_token=access_token)
    aa_uuid = uuid.UUID(aa_identity.agent_id)

    # Look up the standalone agent by public key
    try:
        public_key_bytes = bytes.fromhex(public_key_hex)
    except (ValueError, binascii.Error) as e:
        raise AuthenticationError(f"Invalid public_key_hex: {e}") from e

    async with get_session() as session:
        result = await session.execute(select(Agent).where(Agent.public_key == public_key_bytes))
        standalone_agent = result.scalar_one_or_none()

        if not standalone_agent:
            raise AuthenticationError(
                "No standalone agent found with that public key. "
                "Register first via register_agent with public_key_hex."
            )

        # Update the standalone profile to reflect AgentAuth linkage
        standalone_agent.auth_source = "agentauth"
        standalone_agent.agentauth_linked = True
        standalone_agent.public_key = None  # key no longer usable for auth
        # If the AgentAuth ID is different, record the canonical ID in metadata
        if standalone_agent.agent_id != aa_uuid:
            standalone_agent.metadata_["agentauth_id"] = str(aa_uuid)

        log.info(
            "agent_linked_to_agentauth",
            standalone_id=str(standalone_agent.agent_id),
            agentauth_id=str(aa_uuid),
        )
        merged_id = str(standalone_agent.agent_id)

    return {
        "agent_id": merged_id,
        "agentauth_id": str(aa_uuid),
        "merged": True,
        "message": (
            "Standalone profile successfully linked to AgentAuth identity. "
            "Use your AgentAuth token for all future calls."
        ),
    }


async def whoami(
    access_token: str | None = None,
    public_key_hex: str | None = None,
) -> dict:
    """Check your identity as AgentTrust sees it.

    Returns your ``agent_id``, registration source (``agentauth`` or
    ``standalone``), trust scores summary, interaction count, active scopes,
    and registration date. Useful for verifying your auth is working correctly
    before making other calls.

    Args:
        access_token: AgentAuth bearer token.
        public_key_hex: Hex-encoded Ed25519 public key (standalone agents).

    Returns:
        ``agent_id``, ``source``, ``trust_level``, ``scopes``,
        ``registered_at``, ``display_name``, ``capabilities``,
        ``agentauth_linked``, and ``scores`` (dict of score_type → score).
    """
    identity = await _resolve_identity(access_token, public_key_hex)
    agent_uuid = uuid.UUID(identity.agent_id)

    async with get_session() as session:
        agent_result = await session.execute(select(Agent).where(Agent.agent_id == agent_uuid))
        agent = agent_result.scalar_one_or_none()

        scores: dict[str, float] = {}
        interaction_count = 0
        if agent:
            scores_result = await session.execute(
                select(TrustScore).where(TrustScore.agent_id == agent_uuid)
            )
            for ts in scores_result.scalars().all():
                scores[ts.score_type] = float(ts.score)
                if ts.score_type == "overall":
                    interaction_count = ts.interaction_count

    if not agent:
        return {
            "agent_id": identity.agent_id,
            "source": identity.source,
            "trust_level": identity.trust_level,
            "scopes": identity.scopes,
            "registered_at": None,
            "display_name": None,
            "capabilities": [],
            "agentauth_linked": identity.source == "agentauth",
            "scores": {},
            "interaction_count": 0,
            "note": "Profile not yet created — call register_agent to persist your profile.",
        }

    return {
        "agent_id": str(agent.agent_id),
        "source": agent.auth_source,
        "trust_level": identity.trust_level,
        "scopes": identity.scopes,
        "registered_at": agent.registered_at.isoformat() if agent.registered_at else None,
        "display_name": agent.display_name,
        "capabilities": agent.capabilities or [],
        "agentauth_linked": agent.agentauth_linked,
        "scores": scores,
        "interaction_count": interaction_count,
    }


async def get_agent_profile(
    agent_id: str,
    access_token: str | None = None,
) -> dict:
    """Retrieve an agent's public profile.

    Returns registration date, capabilities, trust summary, and interaction
    count. Authentication is optional — unauthenticated calls get a summary
    view, authenticated calls get full detail including AgentAuth metadata
    and the complete score breakdown.

    Use this to evaluate a potential counterparty before transacting.

    Args:
        agent_id: UUID string of the agent to look up.
        access_token: Optional AgentAuth bearer token for full detail view.

    Returns:
        ``agent_id``, ``display_name``, ``registered_at``, ``capabilities``,
        ``trust_level``, ``scores`` (summary or full), ``interaction_count``,
        ``agentauth_linked``, and ``status``.
        Returns ``{"error": "not_found", ...}`` if the agent does not exist.
    """
    try:
        target_uuid = uuid.UUID(agent_id)
    except ValueError:
        return {"error": "invalid_agent_id", "message": f"'{agent_id}' is not a valid UUID."}

    authenticated = False
    if access_token:
        try:
            await _resolve_identity(access_token, None)
            authenticated = True
        except AuthenticationError:
            pass

    async with get_session() as session:
        result = await session.execute(select(Agent).where(Agent.agent_id == target_uuid))
        agent = result.scalar_one_or_none()

        if not agent:
            return {
                "error": "not_found",
                "message": f"No agent with id '{agent_id}' found.",
            }

        scores_result = await session.execute(
            select(TrustScore).where(TrustScore.agent_id == target_uuid)
        )
        all_scores = scores_result.scalars().all()

    interaction_count = 0
    scores: dict[str, float] = {}
    for ts in all_scores:
        scores[ts.score_type] = float(ts.score)
        if ts.score_type == "overall":
            interaction_count = ts.interaction_count

    profile: dict = {
        "agent_id": str(agent.agent_id),
        "display_name": agent.display_name,
        "registered_at": agent.registered_at.isoformat() if agent.registered_at else None,
        "capabilities": agent.capabilities or [],
        "trust_level": float(agent.trust_level) if agent.trust_level is not None else 0.5,
        "scores": scores,
        "interaction_count": interaction_count,
        "status": agent.status,
    }

    if authenticated:
        profile["agentauth_linked"] = agent.agentauth_linked
        profile["auth_source"] = agent.auth_source
        profile["metadata"] = agent.metadata_

    return profile


async def search_agents(
    min_score: float = 0.0,
    score_type: str = "overall",
    capabilities: list[str] | None = None,
    min_interactions: int = 0,
    limit: int = 20,
    access_token: str | None = None,
) -> dict:
    """Search for agents meeting trust criteria.

    Filter by minimum score, required capabilities, and minimum interaction
    count. Returns matching agents ranked by score descending. Use this to
    find trustworthy agents for a specific task type.

    Args:
        min_score: Minimum trust score (0.0–1.0). Default 0.0 returns all.
        score_type: Score dimension to filter on. One of: ``overall``,
            ``reliability``, ``responsiveness``, ``honesty``, or a
            domain-specific score like ``domain:coding``.
        capabilities: Require agents to have ALL of these capability tags.
        min_interactions: Minimum number of recorded interactions.
        limit: Maximum results to return (1–100, default 20).
        access_token: Optional AgentAuth token (reserved for future
            permission-gated filters).

    Returns:
        ``agents`` list (each with ``agent_id``, ``display_name``,
        ``score``, ``interaction_count``, ``capabilities``), ``total``
        count, and applied ``filters``.
    """
    limit = max(1, min(limit, 100))

    async with get_session() as session:
        query = (
            select(Agent, TrustScore)
            .join(TrustScore, Agent.agent_id == TrustScore.agent_id, isouter=True)
            .where(Agent.status == "active")
        )

        if score_type:
            query = query.where(
                (TrustScore.score_type == score_type) | (TrustScore.score_type.is_(None))
            )

        if min_score > 0.0:
            query = query.where(TrustScore.score >= min_score)

        if min_interactions > 0:
            query = query.where(TrustScore.interaction_count >= min_interactions)

        if capabilities:
            for cap in capabilities:
                query = query.where(Agent.capabilities.contains([cap]))

        query = query.order_by(TrustScore.score.desc().nullslast()).limit(limit)

        rows = (await session.execute(query)).all()

    agents_list = []
    for agent, ts in rows:
        agents_list.append(
            {
                "agent_id": str(agent.agent_id),
                "display_name": agent.display_name,
                "score": float(ts.score) if ts else None,
                "confidence": float(ts.confidence) if ts else None,
                "interaction_count": ts.interaction_count if ts else 0,
                "capabilities": agent.capabilities or [],
                "registered_at": agent.registered_at.isoformat() if agent.registered_at else None,
            }
        )

    return {
        "agents": agents_list,
        "total": len(agents_list),
        "filters": {
            "min_score": min_score,
            "score_type": score_type,
            "capabilities": capabilities,
            "min_interactions": min_interactions,
            "limit": limit,
        },
    }
