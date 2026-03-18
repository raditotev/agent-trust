from __future__ import annotations

import hashlib
from datetime import UTC, datetime
from typing import Any

import jwt
import structlog
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

log = structlog.get_logger()

ALGORITHM = "EdDSA"


def sign_attestation(
    subject_agent_id: str,
    score_snapshot: dict[str, Any],
    valid_from: datetime,
    valid_until: datetime,
    attestation_id: str,
    agentauth_linked: bool = False,
    agent_type: str = "unknown",
    private_key: Ed25519PrivateKey | None = None,
    audience: str | None = None,
) -> str:
    """Sign an attestation JWT with the service Ed25519 key.

    The JWT payload includes:
    - sub: subject agent_id
    - jti: attestation_id (unique identifier)
    - iat: issued at
    - nbf: not before (valid_from)
    - exp: expiry (valid_until)
    - scores: score snapshot dict
    - agentauth_linked: whether the agent has an AgentAuth identity
    - agent_type: type of agent
    - aud: audience (optional, for replay protection)

    A `kid` (Key ID) header is included for key rotation support.
    """
    if private_key is None:
        from agent_trust.crypto.keys import get_service_private_key

        private_key = get_service_private_key()

    # Derive kid from public key fingerprint
    public_key = private_key.public_key()
    pub_bytes = public_key.public_bytes_raw()
    kid = hashlib.sha256(pub_bytes).hexdigest()[:16]

    now = datetime.now(UTC)
    payload: dict[str, Any] = {
        "sub": str(subject_agent_id),
        "jti": str(attestation_id),
        "iat": int(now.timestamp()),
        "nbf": int(valid_from.timestamp()),
        "exp": int(valid_until.timestamp()),
        "iss": "agent-trust",
        "scores": score_snapshot,
        "agentauth_linked": agentauth_linked,
        "agent_type": agent_type,
    }

    if audience:
        payload["aud"] = audience

    token = jwt.encode(
        payload,
        private_key,
        algorithm=ALGORITHM,
        headers={"kid": kid},
    )
    return token


def verify_attestation_jwt(
    token: str,
    public_key=None,
    expected_audience: str | None = None,
) -> dict[str, Any]:
    """Verify an attestation JWT signature and decode the payload.

    Raises jwt.InvalidTokenError subclasses on failure:
    - jwt.ExpiredSignatureError: token is expired
    - jwt.InvalidSignatureError: signature verification failed
    - jwt.DecodeError: malformed token

    Returns the decoded payload dict on success, with ``_kid`` set from the header.
    """
    if public_key is None:
        from agent_trust.crypto.keys import get_service_private_key

        private_key = get_service_private_key()
        public_key = private_key.public_key()

    options: dict[str, bool] = {"verify_exp": True, "verify_nbf": True}
    decode_kwargs: dict[str, Any] = {}
    if expected_audience:
        options["verify_aud"] = True
        decode_kwargs["audience"] = expected_audience
    else:
        options["verify_aud"] = False

    payload = jwt.decode(
        token,
        public_key,
        algorithms=[ALGORITHM],
        options=options,
        **decode_kwargs,
    )

    headers = jwt.get_unverified_header(token)
    payload["_kid"] = headers.get("kid")

    return payload


def decode_attestation_jwt_unverified(token: str) -> dict[str, Any]:
    """Decode a JWT without verifying signature (for inspection only)."""
    return jwt.decode(token, options={"verify_signature": False})
