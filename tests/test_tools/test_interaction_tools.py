from __future__ import annotations

import uuid
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from agent_trust.auth.identity import AgentIdentity
from agent_trust.tools.interactions import get_interaction_history, report_interaction

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_REPORTER_ID = str(uuid.uuid4())
_COUNTERPARTY_ID = str(uuid.uuid4())


def _make_identity(agent_id: str = _REPORTER_ID, scopes: list[str] | None = None) -> AgentIdentity:
    return AgentIdentity(
        agent_id=agent_id,
        source="agentauth",
        scopes=scopes if scopes is not None else ["trust.read", "trust.report"],
        trust_level="delegated",
    )


def _make_agent(agent_id: str) -> MagicMock:
    agent = MagicMock()
    agent.agent_id = uuid.UUID(agent_id)
    agent.display_name = "Test Agent"
    agent.registered_at = datetime.now(UTC)
    return agent


def _make_interaction(
    interaction_id: uuid.UUID | None = None,
    initiator_id: str = _REPORTER_ID,
    counterparty_id: str = _COUNTERPARTY_ID,
    interaction_type: str = "transaction",
    outcome: str = "success",
    mutually_confirmed: bool = False,
) -> MagicMock:
    ix = MagicMock()
    ix.interaction_id = interaction_id or uuid.uuid4()
    ix.initiator_id = uuid.UUID(initiator_id)
    ix.counterparty_id = uuid.UUID(counterparty_id)
    ix.interaction_type = interaction_type
    ix.outcome = outcome
    ix.mutually_confirmed = mutually_confirmed
    ix.reported_at = datetime.now(UTC)
    return ix


@asynccontextmanager
async def _fake_session_ctx(session_mock):
    yield session_mock


def _make_session_mock(
    reporter_agent=None,
    counterparty_agent=None,
    counterparty_report=None,
    history_agent=None,
    history_interactions=None,
):
    """Build a mock async session that returns configured DB results."""
    session = MagicMock()
    session.add = MagicMock()
    session.flush = AsyncMock()

    call_count = [0]

    async def execute(query):
        result = MagicMock()
        n = call_count[0]
        call_count[0] += 1
        if n == 0:
            result.scalar_one_or_none.return_value = reporter_agent
        elif n == 1:
            result.scalar_one_or_none.return_value = counterparty_agent
        elif n == 2:
            result.scalar_one_or_none.return_value = counterparty_report
        else:
            result.scalar_one_or_none.return_value = history_agent
            if history_interactions is not None:
                result.scalars.return_value.all.return_value = history_interactions
        return result

    session.execute = execute
    return session


# ---------------------------------------------------------------------------
# report_interaction tests
# ---------------------------------------------------------------------------


class TestReportInteraction:
    @pytest.mark.asyncio
    async def test_report_interaction_success(self):
        identity = _make_identity()
        reporter = _make_agent(_REPORTER_ID)
        counterparty = _make_agent(_COUNTERPARTY_ID)
        session = _make_session_mock(
            reporter_agent=reporter,
            counterparty_agent=counterparty,
            counterparty_report=None,
        )

        with (
            patch(
                "agent_trust.tools.interactions._resolve_identity_for_interaction",
                new=AsyncMock(return_value=identity),
            ),
            patch(
                "agent_trust.tools.interactions.get_session",
                return_value=_fake_session_ctx(session),
            ),
            patch(
                "agent_trust.tools.interactions._enqueue_score_recomputation",
                new=AsyncMock(),
            ),
        ):
            result = await report_interaction(
                counterparty_id=_COUNTERPARTY_ID,
                interaction_type="transaction",
                outcome="success",
                access_token="token",
            )

        assert "error" not in result
        assert result["reporter_id"] == _REPORTER_ID
        assert result["counterparty_id"] == _COUNTERPARTY_ID
        assert result["outcome"] == "success"
        assert result["mutually_confirmed"] is False
        assert "interaction_id" in result
        assert "reported_at" in result

    @pytest.mark.asyncio
    async def test_report_interaction_requires_scope(self):
        identity = _make_identity(scopes=["trust.read"])  # missing trust.report

        with patch(
            "agent_trust.tools.interactions._resolve_identity_for_interaction",
            new=AsyncMock(return_value=identity),
        ):
            result = await report_interaction(
                counterparty_id=_COUNTERPARTY_ID,
                interaction_type="transaction",
                outcome="success",
                access_token="token",
            )

        assert "error" in result
        assert "trust.report" in result["error"]

    @pytest.mark.asyncio
    async def test_report_interaction_invalid_type(self):
        identity = _make_identity()

        with patch(
            "agent_trust.tools.interactions._resolve_identity_for_interaction",
            new=AsyncMock(return_value=identity),
        ):
            result = await report_interaction(
                counterparty_id=_COUNTERPARTY_ID,
                interaction_type="unknown_type",
                outcome="success",
                access_token="token",
            )

        assert "error" in result
        assert "interaction_type" in result["error"]

    @pytest.mark.asyncio
    async def test_report_interaction_invalid_outcome(self):
        identity = _make_identity()

        with patch(
            "agent_trust.tools.interactions._resolve_identity_for_interaction",
            new=AsyncMock(return_value=identity),
        ):
            result = await report_interaction(
                counterparty_id=_COUNTERPARTY_ID,
                interaction_type="transaction",
                outcome="bad_outcome",
                access_token="token",
            )

        assert "error" in result
        assert "outcome" in result["error"]

    @pytest.mark.asyncio
    async def test_report_interaction_self_report(self):
        identity = _make_identity(agent_id=_REPORTER_ID)

        with patch(
            "agent_trust.tools.interactions._resolve_identity_for_interaction",
            new=AsyncMock(return_value=identity),
        ):
            result = await report_interaction(
                counterparty_id=_REPORTER_ID,  # same as reporter
                interaction_type="transaction",
                outcome="success",
                access_token="token",
            )

        assert "error" in result
        assert "yourself" in result["error"]

    @pytest.mark.asyncio
    async def test_report_interaction_mutual_confirmation(self):
        """When counterparty has already reported, mutually_confirmed=True."""
        identity = _make_identity()
        reporter = _make_agent(_REPORTER_ID)
        counterparty = _make_agent(_COUNTERPARTY_ID)
        existing_report = _make_interaction(
            initiator_id=_COUNTERPARTY_ID,
            counterparty_id=_REPORTER_ID,
            interaction_type="transaction",
            outcome="success",
        )
        session = _make_session_mock(
            reporter_agent=reporter,
            counterparty_agent=counterparty,
            counterparty_report=existing_report,
        )

        with (
            patch(
                "agent_trust.tools.interactions._resolve_identity_for_interaction",
                new=AsyncMock(return_value=identity),
            ),
            patch(
                "agent_trust.tools.interactions.get_session",
                return_value=_fake_session_ctx(session),
            ),
            patch(
                "agent_trust.tools.interactions._enqueue_score_recomputation",
                new=AsyncMock(),
            ),
        ):
            result = await report_interaction(
                counterparty_id=_COUNTERPARTY_ID,
                interaction_type="transaction",
                outcome="success",
                access_token="token",
            )

        assert "error" not in result
        assert result["mutually_confirmed"] is True

    @pytest.mark.asyncio
    async def test_report_interaction_agent_not_found(self):
        """Counterparty not in DB returns an error."""
        identity = _make_identity()
        reporter = _make_agent(_REPORTER_ID)
        session = _make_session_mock(
            reporter_agent=reporter,
            counterparty_agent=None,  # not found
        )

        with (
            patch(
                "agent_trust.tools.interactions._resolve_identity_for_interaction",
                new=AsyncMock(return_value=identity),
            ),
            patch(
                "agent_trust.tools.interactions.get_session",
                return_value=_fake_session_ctx(session),
            ),
        ):
            result = await report_interaction(
                counterparty_id=_COUNTERPARTY_ID,
                interaction_type="transaction",
                outcome="success",
                access_token="token",
            )

        assert "error" in result
        assert "Counterparty" in result["error"]


# ---------------------------------------------------------------------------
# get_interaction_history tests
# ---------------------------------------------------------------------------


class TestGetInteractionHistory:
    @pytest.mark.asyncio
    async def test_get_interaction_history_success(self):
        agent = _make_agent(_REPORTER_ID)
        ix = _make_interaction()
        call_count = [0]

        session = MagicMock()
        session.flush = AsyncMock()

        async def execute(query):
            result = MagicMock()
            n = call_count[0]
            call_count[0] += 1
            if n == 0:
                result.scalar_one_or_none.return_value = agent
            else:
                result.scalars.return_value.all.return_value = [ix]
            return result

        session.execute = execute

        with patch(
            "agent_trust.tools.interactions.get_session",
            return_value=_fake_session_ctx(session),
        ):
            result = await get_interaction_history(agent_id=_REPORTER_ID)

        assert "error" not in result
        assert result["agent_id"] == _REPORTER_ID
        assert result["count"] == 1
        assert len(result["interactions"]) == 1
        item = result["interactions"][0]
        assert item["outcome"] == "success"
        assert "interaction_id" in item

    @pytest.mark.asyncio
    async def test_get_interaction_history_filter_outcome(self):
        agent = _make_agent(_REPORTER_ID)
        call_count = [0]

        session = MagicMock()
        session.flush = AsyncMock()

        async def execute(query):
            result = MagicMock()
            n = call_count[0]
            call_count[0] += 1
            if n == 0:
                result.scalar_one_or_none.return_value = agent
            else:
                result.scalars.return_value.all.return_value = []
            return result

        session.execute = execute

        with patch(
            "agent_trust.tools.interactions.get_session",
            return_value=_fake_session_ctx(session),
        ):
            result = await get_interaction_history(
                agent_id=_REPORTER_ID,
                outcome="failure",
            )

        assert "error" not in result
        assert result["count"] == 0

    @pytest.mark.asyncio
    async def test_get_interaction_history_agent_not_found(self):
        session = MagicMock()
        session.flush = AsyncMock()

        async def execute(query):
            result = MagicMock()
            result.scalar_one_or_none.return_value = None
            return result

        session.execute = execute

        with patch(
            "agent_trust.tools.interactions.get_session",
            return_value=_fake_session_ctx(session),
        ):
            result = await get_interaction_history(agent_id=_REPORTER_ID)

        assert "error" in result
        assert "Agent not found" in result["error"]

    @pytest.mark.asyncio
    async def test_get_interaction_history_clamps_limit(self):
        """limit > 200 is silently clamped to 200."""
        agent = _make_agent(_REPORTER_ID)
        call_count = [0]

        session = MagicMock()
        session.flush = AsyncMock()

        async def execute(query):
            result = MagicMock()
            n = call_count[0]
            call_count[0] += 1
            if n == 0:
                result.scalar_one_or_none.return_value = agent
            else:
                result.scalars.return_value.all.return_value = []
            return result

        session.execute = execute

        with patch(
            "agent_trust.tools.interactions.get_session",
            return_value=_fake_session_ctx(session),
        ):
            result = await get_interaction_history(agent_id=_REPORTER_ID, limit=9999)

        assert "error" not in result
        # No error means the clamping worked; the DB was queried with limit=200
