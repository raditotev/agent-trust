from __future__ import annotations

import time
import uuid
from datetime import UTC, datetime, timedelta

import jwt
import structlog
from sqlalchemy import select

from agent_trust.auth.agentauth import AgentAuthProvider
from agent_trust.auth.identity import AuthenticationError, AuthorizationError
from agent_trust.auth.provider import require_scope
from agent_trust.config import settings
from agent_trust.crypto.jwt import (
    decode_attestation_jwt_unverified,
    sign_attestation,
    verify_attestation_jwt,
)
from agent_trust.db.redis import get_redis
from agent_trust.db.session import get_session
from agent_trust.errors import tool_error
from agent_trust.models import Agent, Attestation, TrustScore

log = structlog.get_logger()


async def issue_attestation(
    agent_id: str,
    access_token: str,
    ttl_hours: int | None = 12,
) -> dict:
    """Issue a signed attestation (JWT) capturing an agent's current trust scores.

    The attestation is portable — the agent can present it to third parties
    who verify the signature without querying this service.

    The JWT includes:
    - sub: agent_id
    - scores: snapshot of all trust scores at issuance time
    - agentauth_linked: whether the agent has an AgentAuth identity
    - iss: 'agent-trust'
    - exp/nbf/iat: validity window

    Attestations are signed with the service's Ed25519 key. Verifiers
    can check the signature using verify_attestation without authentication.

    ttl_hours: validity period in hours (default: ATTESTATION_TTL_HOURS config)
    Requires authentication with trust.attest.issue scope.

    Example call:
        issue_attestation(agent_id="550e8400-...", access_token="eyJ...", ttl_hours=24)

    Example response:
        {
            "attestation_id": "b1c2d3e4-...",
            "subject_agent_id": "550e8400-...",
            "jwt_token": "eyJ...",
            "score_snapshot": {"overall": {"score": 0.82, "confidence": 0.71}},
            "valid_from": "2026-03-20T12:00:00+00:00",
            "valid_until": "2026-03-21T12:00:00+00:00"
        }
    """
    redis = await get_redis()
    provider = AgentAuthProvider(redis_client=redis)
    try:
        identity = await provider.authenticate(access_token=access_token)
        require_scope(identity, "trust.attest.issue")
    except (AuthenticationError, AuthorizationError) as e:
        return tool_error(
            "authentication_failed",
            str(e),
            hint="Provide a valid access_token with trust.attest.issue scope.",
        )

    from agent_trust.ratelimit import check_rate_limit

    rl_result = await check_rate_limit(
        agent_id=identity.agent_id,
        tool_name="issue_attestation",
        trust_level=identity.trust_level,
    )
    if not rl_result.allowed:
        return tool_error(
            "rate_limit_exceeded",
            f"Too many requests. Limit is {rl_result.limit} per minute.",
            hint="Wait and retry after the cooldown period.",
            retry_after_seconds=rl_result.retry_after,
            limit=rl_result.limit,
        )

    ttl = ttl_hours if ttl_hours is not None else settings.attestation_ttl_hours
    ttl = min(max(1, ttl), 72)  # clamp: 1 hour to 3 days

    try:
        subject_uuid = uuid.UUID(agent_id)
    except ValueError:
        return tool_error("invalid_input", f"Invalid agent_id UUID: {agent_id}")

    async with get_session() as session:
        agent_result = await session.execute(select(Agent).where(Agent.agent_id == subject_uuid))
        agent = agent_result.scalar_one_or_none()
        if not agent:
            return tool_error(
                "not_found",
                f"Agent not found: {agent_id}",
                hint="Verify the agent_id or register the agent first.",
            )

        scores_result = await session.execute(
            select(TrustScore).where(TrustScore.agent_id == subject_uuid)
        )
        score_rows = scores_result.scalars().all()
        score_snapshot = {
            row.score_type: {
                "score": float(row.score),
                "confidence": float(row.confidence),
                "interaction_count": row.interaction_count,
            }
            for row in score_rows
        }

        now = datetime.now(UTC)
        valid_until = now + timedelta(hours=ttl)
        attestation_id = uuid.uuid4()

        try:
            token = sign_attestation(
                subject_agent_id=str(subject_uuid),
                score_snapshot=score_snapshot,
                valid_from=now,
                valid_until=valid_until,
                attestation_id=str(attestation_id),
                agentauth_linked=agent.agentauth_linked,
                agent_type=agent.metadata_.get("agent_type", "unknown"),
            )
        except FileNotFoundError:
            return tool_error(
                "signing_failed",
                "Signing key not found.",
                hint=(
                    "Server operator: run scripts/generate_keypair.py to generate the signing key."
                ),
            )

        attestation = Attestation(
            attestation_id=attestation_id,
            subject_id=subject_uuid,
            score_snapshot=score_snapshot,
            valid_from=now,
            valid_until=valid_until,
            jwt_token=token,
            revoked=False,
            created_at=now,
        )
        session.add(attestation)
        await session.flush()

        log.info(
            "attestation_issued",
            attestation_id=str(attestation_id),
            subject_id=agent_id,
            valid_until=valid_until.isoformat(),
            issued_by=identity.agent_id,
        )

        return {
            "attestation_id": str(attestation_id),
            "subject_agent_id": agent_id,
            "jwt_token": token,
            "score_snapshot": score_snapshot,
            "valid_from": now.isoformat(),
            "valid_until": valid_until.isoformat(),
            "agentauth_linked": agent.agentauth_linked,
        }


async def verify_attestation(jwt_token: str) -> dict:
    """Verify an attestation's signature, check expiry, and confirm it hasn't been revoked.

    Returns validity status, the embedded score snapshot, subject agent_id,
    and time remaining until expiry.

    No authentication required — attestations are designed to be portable
    and verifiable by any party without querying this service.
    This is by design: third parties can verify trust claims offline.
    """
    try:
        unverified = decode_attestation_jwt_unverified(jwt_token)
        attestation_id_str = unverified.get("jti")
    except Exception as e:
        return {"valid": False, "error": f"Malformed token: {e}"}

    if attestation_id_str:
        try:
            attestation_uuid = uuid.UUID(attestation_id_str)
            async with get_session() as session:
                result = await session.execute(
                    select(Attestation).where(Attestation.attestation_id == attestation_uuid)
                )
                attestation_record = result.scalar_one_or_none()
                if attestation_record and attestation_record.revoked:
                    return {
                        "valid": False,
                        "error": "Attestation has been revoked",
                        "attestation_id": attestation_id_str,
                    }
        except Exception as e:
            log.warning("attestation_revocation_check_failed", error=str(e))

    try:
        payload = verify_attestation_jwt(jwt_token)
    except jwt.ExpiredSignatureError:
        return {"valid": False, "error": "Attestation has expired"}
    except jwt.InvalidSignatureError:
        return {"valid": False, "error": "Invalid signature"}
    except jwt.DecodeError as e:
        return {"valid": False, "error": f"Decode error: {e}"}
    except Exception as e:
        return {"valid": False, "error": f"Verification failed: {e}"}

    exp = payload.get("exp", 0)
    seconds_remaining = max(0, int(exp - time.time()))

    return {
        "valid": True,
        "attestation_id": payload.get("jti"),
        "subject_agent_id": payload.get("sub"),
        "score_snapshot": payload.get("scores", {}),
        "agentauth_linked": payload.get("agentauth_linked", False),
        "agent_type": payload.get("agent_type", "unknown"),
        "issued_at": datetime.fromtimestamp(payload.get("iat", 0), tz=UTC).isoformat(),
        "valid_until": datetime.fromtimestamp(exp, tz=UTC).isoformat(),
        "seconds_remaining": seconds_remaining,
    }
