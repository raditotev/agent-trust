from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import jwt
import pytest

from agent_trust.crypto.jwt import sign_attestation, verify_attestation_jwt
from agent_trust.crypto.keys import generate_ed25519_keypair, get_public_key_hex


@pytest.fixture
def keypair():
    return generate_ed25519_keypair()


def test_sign_and_verify_attestation(keypair):
    private_key, public_key = keypair
    now = datetime.now(UTC)
    token = sign_attestation(
        subject_agent_id="agent-123",
        score_snapshot={"overall": 0.85, "reliability": 0.90},
        valid_from=now,
        valid_until=now + timedelta(hours=24),
        attestation_id=str(uuid.uuid4()),
        agentauth_linked=True,
        agent_type="tool",
        private_key=private_key,
    )
    assert isinstance(token, str)
    assert len(token) > 50

    payload = verify_attestation_jwt(token, public_key=public_key)
    assert payload["sub"] == "agent-123"
    assert payload["scores"]["overall"] == 0.85
    assert payload["agentauth_linked"] is True
    assert payload["iss"] == "agent-trust"


def test_expired_token_raises(keypair):
    private_key, public_key = keypair
    now = datetime.now(UTC)
    token = sign_attestation(
        subject_agent_id="agent-123",
        score_snapshot={"overall": 0.5},
        valid_from=now - timedelta(hours=2),
        valid_until=now - timedelta(hours=1),  # expired
        attestation_id=str(uuid.uuid4()),
        private_key=private_key,
    )
    with pytest.raises(jwt.ExpiredSignatureError):
        verify_attestation_jwt(token, public_key=public_key)


def test_tampered_token_raises(keypair):
    private_key, public_key = keypair
    now = datetime.now(UTC)
    token = sign_attestation(
        subject_agent_id="agent-123",
        score_snapshot={"overall": 0.5},
        valid_from=now,
        valid_until=now + timedelta(hours=24),
        attestation_id=str(uuid.uuid4()),
        private_key=private_key,
    )
    parts = token.split(".")
    tampered = parts[0] + "." + "tampered_payload_AAAA" + "." + parts[2]
    with pytest.raises(Exception):
        verify_attestation_jwt(tampered, public_key=public_key)


def test_keypair_hex_representation(keypair):
    _, public_key = keypair
    hex_str = get_public_key_hex(public_key)
    assert len(hex_str) == 64  # 32 bytes = 64 hex chars
    assert all(c in "0123456789abcdef" for c in hex_str)


def test_different_key_cannot_verify(keypair):
    private_key, _ = keypair
    _, other_public_key = generate_ed25519_keypair()
    now = datetime.now(UTC)
    token = sign_attestation(
        subject_agent_id="agent-123",
        score_snapshot={"overall": 0.5},
        valid_from=now,
        valid_until=now + timedelta(hours=24),
        attestation_id=str(uuid.uuid4()),
        private_key=private_key,
    )
    with pytest.raises(Exception):
        verify_attestation_jwt(token, public_key=other_public_key)
