from __future__ import annotations

import uuid
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from agent_trust.auth.identity import AgentIdentity
from agent_trust.crypto.jwt import sign_attestation, verify_attestation_jwt
from agent_trust.crypto.keys import generate_ed25519_keypair
from agent_trust.ratelimit import RateLimitResult
from agent_trust.tools.attestations import issue_attestation, verify_attestation

_RATE_LIMIT_ALLOWED = RateLimitResult(
    allowed=True, limit=120, remaining=119, reset_at=9_999_999_999
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_ISSUER_ID = str(uuid.uuid4())
_SUBJECT_ID = str(uuid.uuid4())


def _make_identity(scopes: list[str] | None = None) -> AgentIdentity:
    return AgentIdentity(
        agent_id=_ISSUER_ID,
        source="agentauth",
        scopes=scopes if scopes is not None else ["trust.attest.issue"],
        trust_level="delegated",
    )


def _make_agent(agent_id: str, agentauth_linked: bool = True) -> MagicMock:
    agent = MagicMock()
    agent.agent_id = uuid.UUID(agent_id)
    agent.agentauth_linked = agentauth_linked
    agent.metadata_ = {"agent_type": "assistant"}
    return agent


def _make_score_row(score_type: str = "general", score: float = 0.75) -> MagicMock:
    row = MagicMock()
    row.score_type = score_type
    row.score = score
    row.confidence = 0.8
    row.interaction_count = 10
    return row


@asynccontextmanager
async def _fake_session_ctx(session_mock):
    yield session_mock


def _make_session_for_issue(agent=None, score_rows=None):
    session = MagicMock()
    session.add = MagicMock()
    session.flush = AsyncMock()
    call_count = [0]

    async def execute(query):
        result = MagicMock()
        n = call_count[0]
        call_count[0] += 1
        if n == 0:
            result.scalar_one_or_none.return_value = agent
        else:
            result.scalars.return_value.all.return_value = score_rows or []
        return result

    session.execute = execute
    return session


def _make_session_for_verify(attestation_record=None):
    session = MagicMock()

    async def execute(query):
        result = MagicMock()
        result.scalar_one_or_none.return_value = attestation_record
        return result

    session.execute = execute
    return session


def _sign_test_token(
    subject_id: str,
    private_key,
    ttl_seconds: int = 3600,
    attestation_id: str | None = None,
) -> str:
    now = datetime.now(UTC)
    return sign_attestation(
        subject_agent_id=subject_id,
        score_snapshot={"general": {"score": 0.75, "confidence": 0.8, "interaction_count": 10}},
        valid_from=now,
        valid_until=now + timedelta(seconds=ttl_seconds),
        attestation_id=attestation_id or str(uuid.uuid4()),
        agentauth_linked=True,
        agent_type="assistant",
        private_key=private_key,
    )


# ---------------------------------------------------------------------------
# issue_attestation tests
# ---------------------------------------------------------------------------


class TestIssueAttestation:
    @pytest.mark.asyncio
    async def test_issue_attestation_success(self):
        identity = _make_identity()
        agent = _make_agent(_SUBJECT_ID)
        score_rows = [_make_score_row()]
        session = _make_session_for_issue(agent=agent, score_rows=score_rows)

        fake_token = "header.payload.sig"

        with (
            patch(
                "agent_trust.tools.attestations.AgentAuthProvider",
                return_value=MagicMock(authenticate=AsyncMock(return_value=identity)),
            ),
            patch(
                "agent_trust.tools.attestations.get_redis", new=AsyncMock(return_value=MagicMock())
            ),
            patch(
                "agent_trust.tools.attestations.get_session",
                return_value=_fake_session_ctx(session),
            ),
            patch("agent_trust.tools.attestations.sign_attestation", return_value=fake_token),
            patch(
                "agent_trust.ratelimit.check_rate_limit",
                new=AsyncMock(return_value=_RATE_LIMIT_ALLOWED),
            ),
        ):
            result = await issue_attestation(
                agent_id=_SUBJECT_ID,
                access_token="tok",
            )

        assert "error" not in result
        assert result["subject_agent_id"] == _SUBJECT_ID
        assert result["jwt_token"] == fake_token
        assert "score_snapshot" in result
        assert "attestation_id" in result
        assert result["agentauth_linked"] is True

    @pytest.mark.asyncio
    async def test_issue_attestation_requires_scope(self):
        identity = _make_identity(scopes=["trust.read"])

        with (
            patch(
                "agent_trust.tools.attestations.AgentAuthProvider",
                return_value=MagicMock(authenticate=AsyncMock(return_value=identity)),
            ),
            patch(
                "agent_trust.tools.attestations.get_redis", new=AsyncMock(return_value=MagicMock())
            ),
        ):
            result = await issue_attestation(agent_id=_SUBJECT_ID, access_token="tok")

        assert "error" in result
        assert (
            "scope" in result["error"].lower()
            or "permission" in result["error"].lower()
            or "trust.attest.issue" in result["error"]
        )

    @pytest.mark.asyncio
    async def test_issue_attestation_agent_not_found(self):
        identity = _make_identity()
        session = _make_session_for_issue(agent=None)

        with (
            patch(
                "agent_trust.tools.attestations.AgentAuthProvider",
                return_value=MagicMock(authenticate=AsyncMock(return_value=identity)),
            ),
            patch(
                "agent_trust.tools.attestations.get_redis", new=AsyncMock(return_value=MagicMock())
            ),
            patch(
                "agent_trust.tools.attestations.get_session",
                return_value=_fake_session_ctx(session),
            ),
            patch(
                "agent_trust.ratelimit.check_rate_limit",
                new=AsyncMock(return_value=_RATE_LIMIT_ALLOWED),
            ),
        ):
            result = await issue_attestation(agent_id=_SUBJECT_ID, access_token="tok")

        assert "error" in result
        assert "not found" in result["error"].lower()

    @pytest.mark.asyncio
    async def test_issue_attestation_key_not_found(self):
        identity = _make_identity()
        agent = _make_agent(_SUBJECT_ID)
        score_rows = [_make_score_row()]
        session = _make_session_for_issue(agent=agent, score_rows=score_rows)

        with (
            patch(
                "agent_trust.tools.attestations.AgentAuthProvider",
                return_value=MagicMock(authenticate=AsyncMock(return_value=identity)),
            ),
            patch(
                "agent_trust.tools.attestations.get_redis", new=AsyncMock(return_value=MagicMock())
            ),
            patch(
                "agent_trust.tools.attestations.get_session",
                return_value=_fake_session_ctx(session),
            ),
            patch(
                "agent_trust.tools.attestations.sign_attestation",
                side_effect=FileNotFoundError("keys/service.key not found"),
            ),
            patch(
                "agent_trust.ratelimit.check_rate_limit",
                new=AsyncMock(return_value=_RATE_LIMIT_ALLOWED),
            ),
        ):
            result = await issue_attestation(agent_id=_SUBJECT_ID, access_token="tok")

        assert "error" in result
        assert "signing key" in result["error"].lower() or "generate_keypair" in result["error"]

    @pytest.mark.asyncio
    async def test_issue_attestation_invalid_uuid(self):
        identity = _make_identity()

        with (
            patch(
                "agent_trust.tools.attestations.AgentAuthProvider",
                return_value=MagicMock(authenticate=AsyncMock(return_value=identity)),
            ),
            patch(
                "agent_trust.tools.attestations.get_redis", new=AsyncMock(return_value=MagicMock())
            ),
            patch(
                "agent_trust.ratelimit.check_rate_limit",
                new=AsyncMock(return_value=_RATE_LIMIT_ALLOWED),
            ),
        ):
            result = await issue_attestation(agent_id="not-a-uuid", access_token="tok")

        assert "error" in result
        assert "invalid" in result["error"].lower()


# ---------------------------------------------------------------------------
# verify_attestation tests
# ---------------------------------------------------------------------------


class TestVerifyAttestation:
    @pytest.mark.asyncio
    async def test_verify_attestation_valid(self):
        private_key, public_key = generate_ed25519_keypair()
        attestation_id = str(uuid.uuid4())
        token = _sign_test_token(_SUBJECT_ID, private_key, attestation_id=attestation_id)

        session = _make_session_for_verify(attestation_record=None)

        with (
            patch(
                "agent_trust.tools.attestations.get_session",
                return_value=_fake_session_ctx(session),
            ),
            patch(
                "agent_trust.tools.attestations.verify_attestation_jwt",
                side_effect=lambda t: verify_attestation_jwt(t, public_key=public_key),
            ),
        ):
            result = await verify_attestation(token)

        assert result["valid"] is True
        assert result["subject_agent_id"] == _SUBJECT_ID
        assert result["attestation_id"] == attestation_id
        assert "score_snapshot" in result
        assert result["seconds_remaining"] > 0

    @pytest.mark.asyncio
    async def test_verify_attestation_expired(self):
        private_key, public_key = generate_ed25519_keypair()
        token = _sign_test_token(_SUBJECT_ID, private_key, ttl_seconds=-3600)

        session = _make_session_for_verify(attestation_record=None)

        with (
            patch(
                "agent_trust.tools.attestations.get_session",
                return_value=_fake_session_ctx(session),
            ),
            patch(
                "agent_trust.tools.attestations.verify_attestation_jwt",
                side_effect=lambda t: verify_attestation_jwt(t, public_key=public_key),
            ),
        ):
            result = await verify_attestation(token)

        assert result["valid"] is False
        assert "expired" in result["error"].lower()

    @pytest.mark.asyncio
    async def test_verify_attestation_tampered(self):
        private_key, public_key = generate_ed25519_keypair()
        token = _sign_test_token(_SUBJECT_ID, private_key)
        # Tamper with the payload segment
        parts = token.split(".")
        tampered = parts[0] + "." + parts[1] + "TAMPERED" + "." + parts[2]

        session = _make_session_for_verify(attestation_record=None)

        with (
            patch(
                "agent_trust.tools.attestations.get_session",
                return_value=_fake_session_ctx(session),
            ),
            patch(
                "agent_trust.tools.attestations.verify_attestation_jwt",
                side_effect=lambda t: verify_attestation_jwt(t, public_key=public_key),
            ),
        ):
            result = await verify_attestation(tampered)

        assert result["valid"] is False

    @pytest.mark.asyncio
    async def test_verify_attestation_revoked(self):
        private_key, public_key = generate_ed25519_keypair()
        attestation_id = str(uuid.uuid4())
        token = _sign_test_token(_SUBJECT_ID, private_key, attestation_id=attestation_id)

        revoked_record = MagicMock()
        revoked_record.revoked = True
        session = _make_session_for_verify(attestation_record=revoked_record)

        with (
            patch(
                "agent_trust.tools.attestations.get_session",
                return_value=_fake_session_ctx(session),
            ),
            patch(
                "agent_trust.tools.attestations.verify_attestation_jwt",
                side_effect=lambda t: verify_attestation_jwt(t, public_key=public_key),
            ),
        ):
            result = await verify_attestation(token)

        assert result["valid"] is False
        assert "revoked" in result["error"].lower()

    @pytest.mark.asyncio
    async def test_verify_attestation_malformed(self):
        result = await verify_attestation("this.is.garbage")
        assert result["valid"] is False
        assert "error" in result
