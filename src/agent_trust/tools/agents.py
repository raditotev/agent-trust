from __future__ import annotations

import binascii
import json as _json
import time
import uuid
from datetime import UTC, datetime

import jwt as pyjwt
import structlog
from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PrivateKey,
    Ed25519PublicKey,
)
from sqlalchemy import or_, select

from agent_trust.auth.agentauth import AgentAuthProvider
from agent_trust.auth.identity import AgentIdentity, AuthenticationError
from agent_trust.auth.standalone import STANDALONE_SCOPES
from agent_trust.config import settings
from agent_trust.db.redis import get_redis
from agent_trust.db.session import get_session
from agent_trust.errors import (
    ALREADY_LINKED,
    INVALID_INPUT,
    KEY_NOT_FOUND,
    PROOF_EXPIRED,
    PROOF_SIG_INVALID,
    tool_error,
)
from agent_trust.models import Agent, Attestation, Interaction, TrustScore
from agent_trust.ratelimit import check_rate_limit

log = structlog.get_logger()


async def _check_delegation_cycle(
    proposed_child_id: uuid.UUID,
    proposed_parent_id: uuid.UUID,
    session,
) -> bool:
    """Return True if setting delegated_by=proposed_parent would create
    a cycle or exceed depth 5.
    """
    from sqlalchemy import select

    from agent_trust.models import Agent

    visited: set[uuid.UUID] = {proposed_child_id}
    current_id = proposed_parent_id
    depth = 0

    while current_id and depth < 10:
        if current_id in visited:
            return True  # cycle detected
        visited.add(current_id)
        depth += 1
        if depth > 5:
            return True  # chain too deep

        result = await session.execute(
            select(Agent.delegated_by).where(Agent.agent_id == current_id)
        )
        current_id = result.scalar_one_or_none()

    return False


async def _resolve_identity(
    access_token: str | None,
    public_key_hex: str | None,
) -> AgentIdentity:
    """Resolve agent identity from AgentAuth token, standalone signed JWT, or public key."""
    from agent_trust.auth.resolve import resolve_identity

    return await resolve_identity(access_token=access_token, public_key_hex=public_key_hex)


async def _ensure_agent_profile(
    agent_id: uuid.UUID,
    auth_source: str,
    display_name: str | None = None,
    capabilities: list[str] | None = None,
    metadata: dict | None = None,
    public_key: bytes | None = None,
) -> tuple[Agent, bool]:
    """Get or create an agent profile. Returns (agent, created).

    NOTE: If delegated_by is ever set via this function or elsewhere,
    it MUST be validated using _check_delegation_cycle() to prevent
    infinite delegation chains and cycles.
    """
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

    Example call (auto-generated keys — simplest path):
        register_agent(display_name="my-search-agent", capabilities=["search", "summarize"])

    Example response:
        {
            "agent_id": "550e8400-...",
            "source": "standalone",
            "scopes": ["trust.read", "trust.report"],
            "created": true,
            "public_key_hex": "a1b2c3...",
            "private_key_hex": "d4e5f6...",
            "warning": "Key pair auto-generated. Store private_key_hex securely."
        }
    """
    # --- Rate limiting (before any DB work) ---
    _rl_agent_id: str | None = None
    _rl_trust_level: str | None = None
    if access_token:
        try:
            _rl_identity = await _resolve_identity(access_token, None)
            _rl_agent_id = _rl_identity.agent_id
            _rl_trust_level = _rl_identity.trust_level
        except AuthenticationError:
            pass  # fall through to anonymous rate limit

    rl_result = await check_rate_limit(
        agent_id=_rl_agent_id,
        tool_name="register_agent",
        trust_level=_rl_trust_level,
    )
    if not rl_result.allowed:
        return {
            "error": "Rate limit exceeded",
            "retry_after_seconds": rl_result.retry_after,
            "limit": rl_result.limit,
        }

    # Input size validation
    if display_name is not None and len(display_name) > 200:
        return {"error": "display_name too long: maximum 200 characters"}

    if capabilities is not None:
        if len(capabilities) > 50:
            return {"error": "Too many capabilities: maximum 50"}
        for cap in capabilities:
            if not isinstance(cap, str) or len(cap) > 100:
                return {"error": "Each capability must be a string of max 100 characters"}

    if metadata is not None:
        try:
            metadata_size = len(_json.dumps(metadata).encode("utf-8"))
        except (TypeError, ValueError):
            return {"error": "metadata must be a JSON-serializable object"}
        if metadata_size > 10240:
            return {"error": f"metadata payload too large: {metadata_size} bytes (max 10240)"}

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
    signed_proof: str,
    dry_run: bool = False,
) -> dict:
    """Link a standalone trust profile to an AgentAuth identity.

    Provide your AgentAuth ``access_token``, the ``public_key_hex`` you
    originally registered with, and a ``signed_proof`` JWT proving you own
    the standalone agent's private key. Your interaction history and scores
    transfer to the AgentAuth identity. The standalone profile is updated to
    reflect the AgentAuth source. This is a one-time, irreversible operation.

    After linking, authenticate exclusively with your AgentAuth token — the
    public key will no longer be usable for authentication.

    **Canonical agent ID contract**: after a successful link the canonical
    ``agent_id`` is always the original standalone UUID. The AgentAuth UUID
    is stored as ``agentauth_id`` in the profile's metadata and is also
    returned in the response as ``agentauth_id``. All historical scores and
    interactions remain attached to the canonical standalone UUID.

    The ``signed_proof`` JWT must be signed with the standalone agent's
    Ed25519 private key (algorithm ``EdDSA``) and contain the following
    claims:

    - ``sub``: your ``public_key_hex`` (hex-encoded 32-byte public key)
    - ``action``: the literal string ``"link_agentauth"``
    - ``iat``: issued-at Unix timestamp (must be within 300 seconds of now)

    Example (Python)::

        import jwt, time
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

        private_key = Ed25519PrivateKey.from_private_bytes(bytes.fromhex(private_key_hex))
        signed_proof = jwt.encode(
            {"sub": public_key_hex, "action": "link_agentauth", "iat": int(time.time())},
            private_key,
            algorithm="EdDSA",
        )

    Args:
        access_token: Valid AgentAuth bearer token identifying the target identity.
        public_key_hex: Hex-encoded Ed25519 public key used during standalone registration.
        signed_proof: JWT signed by the standalone agent's Ed25519 private key,
            proving ownership of the key. Must contain ``sub``, ``action``, and
            ``iat`` claims as described above.
        dry_run: When ``True``, validate everything but do **not** commit any
            changes. Returns a preview of what would happen including current
            scores. Defaults to ``False``.

    Returns:
        On success: ``agent_id`` (canonical standalone UUID), ``canonical_agent_id``
        (same as ``agent_id``), ``agentauth_id`` (AgentAuth UUID stored in
        metadata), ``merged`` (bool), and a confirmation ``message``.

        On ``dry_run=True``: ``dry_run`` (``true``), ``would_link_agent_id``,
        ``agentauth_id``, ``current_scores``, ``interaction_count``,
        ``capabilities``, and ``message``.

    Error codes:
        - ``invalid_input``: malformed ``public_key_hex``.
        - ``proof_sig_invalid``: JWT signature or content check failed.
        - ``proof_expired``: JWT ``iat`` is outside the 300-second window.
        - ``key_not_found``: no standalone agent registered with that public key.
        - ``already_linked``: the standalone profile is already linked to AgentAuth.
        - ``authentication_failed``: AgentAuth token invalid or expired.
    """
    # --- Validate public key ---
    try:
        public_key_bytes = bytes.fromhex(public_key_hex)
    except (ValueError, binascii.Error) as e:
        return tool_error(INVALID_INPUT, f"Invalid public_key_hex: {e}")

    try:
        public_key = Ed25519PublicKey.from_public_bytes(public_key_bytes)
    except Exception as e:
        return tool_error(INVALID_INPUT, f"Cannot decode public key: {e}")

    # --- Verify ownership proof JWT ---
    try:
        payload = pyjwt.decode(
            signed_proof,
            public_key,
            algorithms=["EdDSA"],
            options={"require": ["sub", "action", "iat"]},
        )
    except pyjwt.InvalidTokenError as e:
        return tool_error(
            PROOF_SIG_INVALID,
            f"JWT signature verification failed: {e}",
            hint="Re-sign a fresh proof JWT with your standalone private key.",
        )

    if payload.get("sub") != public_key_hex:
        return tool_error(
            PROOF_SIG_INVALID,
            "Proof 'sub' does not match public_key_hex.",
        )

    if payload.get("action") != "link_agentauth":
        return tool_error(
            PROOF_SIG_INVALID,
            "Proof 'action' must be 'link_agentauth'.",
        )

    iat = payload.get("iat")
    if not isinstance(iat, int | float) or abs(time.time() - iat) > 300:
        return tool_error(
            PROOF_EXPIRED,
            "Proof 'iat' is missing or outside the 300-second window.",
            hint="Generate a new signed_proof with a fresh iat timestamp.",
        )

    # --- Verify AgentAuth token ---
    redis = await get_redis()
    aa_provider = AgentAuthProvider(redis_client=redis)
    aa_identity = await aa_provider.authenticate(access_token=access_token)
    aa_uuid = uuid.UUID(aa_identity.agent_id)

    # --- Look up standalone agent and apply (or preview) the link ---
    async with get_session() as session:
        result = await session.execute(select(Agent).where(Agent.public_key == public_key_bytes))
        standalone_agent = result.scalar_one_or_none()

        if not standalone_agent:
            return tool_error(
                KEY_NOT_FOUND,
                "No standalone agent found with that public key.",
                hint="Register first via register_agent with public_key_hex.",
            )

        if standalone_agent.agentauth_linked or standalone_agent.auth_source == "agentauth":
            return tool_error(
                ALREADY_LINKED,
                "This standalone profile is already linked to an AgentAuth identity.",
                hint="Use your AgentAuth token to authenticate. Re-linking is not supported.",
            )

        standalone_id = str(standalone_agent.agent_id)
        standalone_uuid = standalone_agent.agent_id

        if dry_run:
            scores_result = await session.execute(
                select(TrustScore).where(TrustScore.agent_id == standalone_uuid)
            )
            current_scores: dict[str, float] = {}
            interaction_count = 0
            for ts in scores_result.scalars().all():
                current_scores[ts.score_type] = float(ts.score)
                if ts.score_type == "overall":
                    interaction_count = ts.interaction_count
            return {
                "dry_run": True,
                "would_link_agent_id": standalone_id,
                "agentauth_id": str(aa_uuid),
                "current_scores": current_scores,
                "interaction_count": interaction_count,
                "capabilities": standalone_agent.capabilities or [],
                "message": "dry_run: no changes made",
            }

        # Mutate the standalone profile to reflect AgentAuth linkage
        standalone_agent.auth_source = "agentauth"
        standalone_agent.agentauth_linked = True
        standalone_agent.public_key = None  # key no longer usable for auth
        # Reassign the full dict so SQLAlchemy's change tracker detects the mutation
        # (in-place JSONB mutations are not tracked). Guards against metadata_ being None.
        if standalone_uuid != aa_uuid:
            current_metadata = standalone_agent.metadata_ or {}
            standalone_agent.metadata_ = {**current_metadata, "agentauth_id": str(aa_uuid)}

        log.info(
            "agent_linked_to_agentauth",
            standalone_id=standalone_id,
            agentauth_id=str(aa_uuid),
        )

    return {
        "agent_id": standalone_id,
        "canonical_agent_id": standalone_id,
        "agentauth_id": str(aa_uuid),
        "merged": True,
        "message": (
            "Standalone profile successfully linked to AgentAuth identity. "
            "canonical_agent_id is the original standalone UUID; "
            "agentauth_id is the foreign AgentAuth reference stored in metadata. "
            "Use your AgentAuth token for all future calls."
        ),
    }


async def verify_link_proof(
    access_token: str,
    public_key_hex: str,
    signed_proof: str,
) -> dict:
    """Preflight check: validate a link_agentauth proof without writing to the DB.

    Runs all the same validation steps as ``link_agentauth`` — token
    authenticity, key lookup, proof signature, expiry, and whether the
    agent is already linked — but never persists any changes.  Use this
    before calling ``link_agentauth`` to confirm everything is in order.

    Args:
        access_token: AgentAuth bearer token to validate.
        public_key_hex: Hex-encoded Ed25519 public key of the standalone agent.
        signed_proof: JWT signed by the standalone private key (same format
            required by ``link_agentauth``).

    Returns:
        A dict with:

        - ``valid`` (bool): ``True`` only when all checks pass.
        - ``checks``: individual check results:

          - ``token_valid`` — AgentAuth token successfully authenticated.
          - ``key_found`` — standalone agent exists with this public key.
          - ``proof_sig_valid`` — JWT signature, ``sub``, and ``action`` OK.
          - ``proof_not_expired`` — ``iat`` within 300 seconds of now.
          - ``already_linked`` — the agent is already linked (blocks linking).

        - ``agent_id``: standalone UUID if the key was found, else omitted.
        - ``error``: description of the first failing check, if ``valid=False``.
    """
    checks: dict[str, bool] = {
        "token_valid": False,
        "key_found": False,
        "proof_sig_valid": False,
        "proof_not_expired": False,
        "already_linked": False,
    }
    first_error: str | None = None

    # --- Validate public key bytes ---
    try:
        public_key_bytes = bytes.fromhex(public_key_hex)
        public_key = Ed25519PublicKey.from_public_bytes(public_key_bytes)
    except Exception as e:
        return {"valid": False, "checks": checks, "error": f"Invalid public_key_hex: {e}"}

    # --- Verify proof JWT signature, sub, and action ---
    proof_payload: dict | None = None
    try:
        proof_payload = pyjwt.decode(
            signed_proof,
            public_key,
            algorithms=["EdDSA"],
            options={"require": ["sub", "action", "iat"]},
        )
        if (
            proof_payload.get("sub") == public_key_hex
            and proof_payload.get("action") == "link_agentauth"
        ):
            checks["proof_sig_valid"] = True
        else:
            first_error = first_error or "Proof 'sub' or 'action' claim is incorrect."
    except pyjwt.InvalidTokenError as e:
        first_error = first_error or f"JWT signature verification failed: {e}"

    # --- Check iat expiry (only if sig was valid) ---
    if proof_payload is not None:
        iat = proof_payload.get("iat")
        if isinstance(iat, int | float) and abs(time.time() - iat) <= 300:
            checks["proof_not_expired"] = True
        else:
            first_error = first_error or "Proof 'iat' is missing or outside the 300-second window."

    # --- Verify AgentAuth token ---
    try:
        redis = await get_redis()
        aa_provider = AgentAuthProvider(redis_client=redis)
        await aa_provider.authenticate(access_token=access_token)
        checks["token_valid"] = True
    except Exception as e:
        first_error = first_error or f"AgentAuth token invalid: {e}"

    # --- Look up standalone agent ---
    agent_id_str: str | None = None
    async with get_session() as session:
        result = await session.execute(select(Agent).where(Agent.public_key == public_key_bytes))
        standalone_agent = result.scalar_one_or_none()

        if standalone_agent:
            checks["key_found"] = True
            agent_id_str = str(standalone_agent.agent_id)
            if standalone_agent.agentauth_linked or standalone_agent.auth_source == "agentauth":
                checks["already_linked"] = True
                first_error = first_error or "Agent is already linked to an AgentAuth identity."
        else:
            first_error = first_error or "No standalone agent found with that public key."

    valid = (
        checks["token_valid"]
        and checks["key_found"]
        and checks["proof_sig_valid"]
        and checks["proof_not_expired"]
        and not checks["already_linked"]
    )

    result_dict: dict = {"valid": valid, "checks": checks}
    if agent_id_str:
        result_dict["agent_id"] = agent_id_str
    if not valid and first_error:
        result_dict["error"] = first_error
    return result_dict


async def generate_agent_token(
    agent_id: str,
    private_key_hex: str,
    ttl_minutes: int = 60,
) -> dict:
    """Generate a signed access token for a standalone agent.

    Standalone agents authenticate by signing short-lived JWTs with their
    Ed25519 private key. Call this tool to obtain an access_token that can
    be passed directly to report_interaction, file_dispute, issue_attestation,
    get_score_breakdown, and any other tool requiring authentication.

    The token is signed using your private key — no key material is stored
    server-side. When your token expires, call this tool again.

    Typical agent flow:
      1. Call register_agent() once → receive agent_id + private_key_hex
      2. Call generate_agent_token(agent_id, private_key_hex) → receive access_token
      3. Pass access_token to authenticated tools
      4. Repeat step 2 when the token expires (check expires_at)

    Args:
        agent_id: Your agent UUID (from register_agent).
        private_key_hex: Your 32-byte Ed25519 private key as 64 hex chars
                         (private_key_hex from your register_agent response).
        ttl_minutes: Token lifetime in minutes. Default 60, max 1440 (24 h).

    Returns:
        access_token: Signed JWT — pass this as access_token to other tools.
        expires_at: ISO 8601 UTC timestamp when the token expires.
        ttl_minutes: Actual TTL applied after clamping.

    Example call:
        generate_agent_token(
            agent_id="550e8400-...",
            private_key_hex="d4e5f6...",
            ttl_minutes=60
        )

    Example response:
        {
            "access_token": "eyJ...",
            "expires_at": "2026-03-20T13:00:00+00:00",
            "ttl_minutes": 60,
            "agent_id": "550e8400-..."
        }
    """
    from datetime import UTC, timedelta

    from agent_trust.crypto.agent_token import sign_agent_token

    ttl_minutes = min(max(1, ttl_minutes), 1440)

    try:
        token = sign_agent_token(agent_id, private_key_hex, ttl_minutes)
    except AuthenticationError as e:
        return {"error": str(e)}
    except Exception as e:
        return {"error": f"Token generation failed: {e}"}

    from datetime import datetime

    expires_at = datetime.now(UTC) + timedelta(minutes=ttl_minutes)
    return {
        "access_token": token,
        "expires_at": expires_at.isoformat(),
        "ttl_minutes": ttl_minutes,
        "agent_id": agent_id,
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

    **Canonical agent ID contract**: for agents that have linked a standalone
    profile to AgentAuth via ``link_agentauth``, the ``agent_id`` returned
    here is always the original standalone UUID. The AgentAuth UUID is stored
    as ``agentauth_id`` in the profile's ``metadata_`` field. Use the
    standalone UUID (canonical ID) as the stable identifier in all API calls.

    Args:
        access_token: AgentAuth bearer token.
        public_key_hex: Hex-encoded Ed25519 public key (standalone agents).

    Returns:
        ``agent_id`` (canonical standalone UUID), ``source``, ``trust_level``,
        ``scopes``, ``registered_at``, ``display_name``, ``capabilities``,
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


async def agent_status(
    access_token: str | None = None,
    public_key_hex: str | None = None,
) -> dict:
    """Return a comprehensive status snapshot for your agent.

    Combines identity, trust scores, pending confirmation count, and active
    attestations in a single call — useful as a dashboard or health check.

    REQUIRES authentication (access_token or public_key_hex).

    Example call:
        agent_status(access_token="eyJ...")

    Example response:
        {
            "agent_id": "550e8400-...",
            "agentauth_linked": true,
            "scores": {"overall": 0.73, "reliability": 0.81},
            "scopes": ["trust.read", "trust.write"],
            "pending_confirmations": 2,
            "active_attestations": [
                {
                    "attestation_id": "b1c2d3e4-...",
                    "valid_until": "2026-03-21T12:00:00+00:00",
                    "seconds_remaining": 86400
                }
            ]
        }
    """
    identity = await _resolve_identity(access_token, public_key_hex)
    agent_uuid = uuid.UUID(identity.agent_id)
    now = datetime.now(UTC)

    async with get_session() as session:
        agent_result = await session.execute(select(Agent).where(Agent.agent_id == agent_uuid))
        agent = agent_result.scalar_one_or_none()

        scores: dict[str, float] = {}
        if agent:
            scores_result = await session.execute(
                select(TrustScore).where(TrustScore.agent_id == agent_uuid)
            )
            for ts in scores_result.scalars().all():
                scores[ts.score_type] = float(ts.score)

        # Count interactions that still need confirmation from us or our counterparty
        pending_count_result = await session.execute(
            select(Interaction).where(
                or_(
                    Interaction.initiator_id == agent_uuid,
                    Interaction.counterparty_id == agent_uuid,
                ),
                Interaction.reported_by != agent_uuid,
                Interaction.mutually_confirmed == False,  # noqa: E712
            )
        )
        pending_confirmations = len(pending_count_result.scalars().all())

        # Active (non-expired, non-revoked) attestations
        attest_result = await session.execute(
            select(Attestation).where(
                Attestation.subject_id == agent_uuid,
                Attestation.revoked == False,  # noqa: E712
                Attestation.valid_until > now,
            )
        )
        attestation_records = attest_result.scalars().all()

    active_attestations = []
    for rec in attestation_records:
        seconds_remaining = max(0, int((rec.valid_until - now).total_seconds()))
        active_attestations.append(
            {
                "attestation_id": str(rec.attestation_id),
                "valid_until": rec.valid_until.isoformat(),
                "seconds_remaining": seconds_remaining,
            }
        )

    return {
        "agent_id": identity.agent_id,
        "agentauth_linked": agent.agentauth_linked if agent else (identity.source == "agentauth"),
        "scores": scores,
        "scopes": identity.scopes,
        "pending_confirmations": pending_confirmations,
        "active_attestations": active_attestations,
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
