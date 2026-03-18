from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

import jwt
import structlog
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey, Ed25519PublicKey

from agent_trust.auth.identity import AuthenticationError

log = structlog.get_logger()

ALGORITHM = "EdDSA"
STANDALONE_TOKEN_AUDIENCE = "agent-trust"


def sign_agent_token(
    agent_id: str,
    private_key_hex: str,
    ttl_minutes: int = 60,
) -> str:
    """Sign a short-lived access token with the agent's Ed25519 private key.

    The token is a self-issued JWT: iss == sub == agent_id. The server detects
    this pattern, looks up the registered public key, and verifies the signature.

    Args:
        agent_id: The agent's UUID string (must match the registered agent).
        private_key_hex: The agent's 32-byte Ed25519 private key as hex (64 chars).
        ttl_minutes: Token lifetime in minutes (default 60).

    Returns:
        Signed JWT string suitable for use as access_token.
    """
    private_key = _private_key_from_hex(private_key_hex)
    now = datetime.now(UTC)
    payload = {
        "sub": agent_id,
        "iss": agent_id,  # self-issued — used to detect standalone tokens server-side
        "aud": STANDALONE_TOKEN_AUDIENCE,
        "iat": int(now.timestamp()),
        "exp": int((now + timedelta(minutes=ttl_minutes)).timestamp()),
        "jti": str(uuid.uuid4()),  # replay protection
    }
    return jwt.encode(payload, private_key, algorithm=ALGORITHM)


def verify_agent_token(token: str, public_key: Ed25519PublicKey) -> dict[str, Any]:
    """Verify an agent-signed JWT against the agent's registered public key.

    Raises jwt.InvalidTokenError subclasses on failure.
    Returns the decoded payload dict on success.
    """
    return jwt.decode(
        token,
        public_key,
        algorithms=[ALGORITHM],
        audience=STANDALONE_TOKEN_AUDIENCE,
        options={"verify_exp": True},
    )


def is_standalone_agent_token(token: str) -> bool:
    """Return True if the token is a self-issued standalone agent JWT.

    Detection heuristic: the token must be a valid JWT where iss == sub
    (both equal to an agent_id UUID) and aud == "agent-trust". No signature
    verification is performed here — that happens in StandaloneProvider.
    """
    try:
        unverified = jwt.decode(token, options={"verify_signature": False})
        sub = unverified.get("sub", "")
        iss = unverified.get("iss", "")
        aud = unverified.get("aud", "")
        # Normalise aud — PyJWT may return a list when verifying but a string here
        if isinstance(aud, list):
            aud = aud[0] if aud else ""
        return (
            bool(sub)
            and sub == iss
            and aud == STANDALONE_TOKEN_AUDIENCE
            and _is_uuid(sub)
        )
    except Exception:
        return False


def public_key_from_bytes(raw_bytes: bytes) -> Ed25519PublicKey:
    """Reconstruct an Ed25519PublicKey from raw 32-byte representation."""
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey as _K

    # cryptography library accepts raw bytes via from_public_bytes
    return _K.from_public_bytes(raw_bytes)


def _private_key_from_hex(private_key_hex: str) -> Ed25519PrivateKey:
    """Load an Ed25519 private key from its 32-byte hex representation."""
    try:
        raw = bytes.fromhex(private_key_hex)
    except ValueError as e:
        raise AuthenticationError(f"Invalid private_key_hex format: {e}") from e
    return Ed25519PrivateKey.from_private_bytes(raw)


def _is_uuid(value: str) -> bool:
    try:
        uuid.UUID(value)
        return True
    except (ValueError, AttributeError):
        return False
