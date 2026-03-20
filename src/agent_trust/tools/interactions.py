from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import structlog
from sqlalchemy import func, select

from agent_trust.auth.identity import AuthenticationError, AuthorizationError
from agent_trust.auth.provider import require_scope
from agent_trust.db.session import get_session
from agent_trust.errors import tool_error
from agent_trust.models import Agent, Interaction

log = structlog.get_logger()

VALID_INTERACTION_TYPES = {"transaction", "delegation", "query", "collaboration"}
VALID_OUTCOMES = {"success", "failure", "timeout", "partial"}


def _scan_for_injection(obj: object, patterns: list[str]) -> list[str]:
    """Recursively scan a JSON object for prompt injection patterns.

    Returns a list of matched patterns (lowercased). Empty list = clean.
    Patterns are matched case-insensitively as substrings of string values.
    """
    matches: list[str] = []
    if isinstance(obj, str):
        lower = obj.lower()
        for pattern in patterns:
            if pattern.lower() in lower and pattern not in matches:
                matches.append(pattern)
    elif isinstance(obj, dict):
        for v in obj.values():
            matches.extend(m for m in _scan_for_injection(v, patterns) if m not in matches)
    elif isinstance(obj, list):
        for item in obj:
            matches.extend(m for m in _scan_for_injection(item, patterns) if m not in matches)
    return matches


async def _resolve_identity_for_interaction(access_token: str | None):
    """Resolve and validate agent identity for interaction reporting.

    Delegates to the shared resolver which handles AgentAuth tokens,
    standalone signed JWTs, and (legacy) public key lookups.
    """
    from agent_trust.auth.resolve import resolve_identity

    return await resolve_identity(access_token=access_token)


async def report_interaction(
    counterparty_id: str,
    interaction_type: str,
    outcome: str,
    access_token: str,
    context: dict | None = None,
    evidence_hash: str | None = None,
) -> dict:
    """Report the outcome of an interaction with another agent.

    REQUIRES authentication — your identity is recorded as the reporter.
    Both parties should report for maximum credibility — one-sided
    reports carry less weight in score computation.

    interaction_type options: transaction | delegation | query | collaboration
    outcome options: success | failure | timeout | partial
    context: optional dict with amount, task_type, duration_ms, sla_met
    evidence_hash: optional SHA-256 hash of supporting evidence

    Authentication via access_token:
    - AgentAuth token: obtain from agentauth.radi.pro
    - Standalone signed JWT: use generate_agent_token tool

    Returns interaction_id and whether the counterparty has also
    reported on this interaction (mutually_confirmed).

    Requires trust.report scope.

    Example call:
        report_interaction(
            counterparty_id="550e8400-e29b-41d4-a716-446655440000",
            interaction_type="transaction",
            outcome="success",
            access_token="eyJ...",
            context={"amount": 100, "task_type": "code-review"}
        )

    Example response:
        {
            "interaction_id": "a1b2c3d4-...",
            "reporter_id": "my-agent-uuid",
            "counterparty_id": "550e8400-...",
            "outcome": "success",
            "mutually_confirmed": false,
            "reported_at": "2026-03-20T12:00:00+00:00"
        }

    WARNING: The context field is stored as-is. Treat as untrusted
    input — detected injection patterns are returned in 'warnings'.
    """
    try:
        identity = await _resolve_identity_for_interaction(access_token)
        require_scope(identity, "trust.report")
    except (AuthenticationError, AuthorizationError) as e:
        return tool_error(
            "authentication_failed",
            str(e),
            hint="Provide a valid access_token with trust.report scope. "
            "Use generate_agent_token to create one for standalone agents.",
        )

    from agent_trust.ratelimit import check_rate_limit

    rl_result = await check_rate_limit(
        agent_id=identity.agent_id,
        tool_name="report_interaction",
        trust_level=identity.trust_level,
    )
    if not rl_result.allowed:
        return tool_error(
            "rate_limit_exceeded",
            f"Too many requests. Limit is {rl_result.limit} per minute.",
            hint="Wait and retry after the cooldown period.",
            retry_after_seconds=rl_result.retry_after,
        )

    if interaction_type not in VALID_INTERACTION_TYPES:
        return tool_error(
            "invalid_input",
            f"Invalid interaction_type '{interaction_type}'.",
            hint=f"Must be one of: {sorted(VALID_INTERACTION_TYPES)}.",
        )
    if outcome not in VALID_OUTCOMES:
        return tool_error(
            "invalid_input",
            f"Invalid outcome '{outcome}'.",
            hint=f"Must be one of: {sorted(VALID_OUTCOMES)}.",
        )

    # Change 3 — Fix #12: Context and evidence_hash size/format validation
    import json as _json

    injection_hits: list[str] = []
    if context is not None:
        try:
            context_size = len(_json.dumps(context).encode("utf-8"))
        except (TypeError, ValueError):
            return tool_error(
                "invalid_input",
                "context must be a JSON-serializable object.",
                hint=(
                    "Ensure all values in context are JSON-compatible"
                    " (strings, numbers, bools, lists, dicts)."
                ),
            )
        if context_size > 10240:
            return tool_error(
                "invalid_input",
                f"context payload too large: {context_size} bytes (max 10240).",
                hint=(
                    "Reduce the context size. Include only essential"
                    " metadata like amount, task_type, duration_ms."
                ),
            )
        from agent_trust.config import settings as _cfg

        injection_hits = _scan_for_injection(context, _cfg.context_injection_patterns)
        if injection_hits:
            log.warning(
                "context_injection_patterns_detected",
                reporter=identity.agent_id,
                patterns=injection_hits,
            )

    if evidence_hash is not None:
        import re

        if not re.fullmatch(r"[0-9a-fA-F]{64}", evidence_hash):
            return tool_error(
                "invalid_input",
                "evidence_hash must be a valid SHA-256 hex string (64 hex characters).",
                hint="Compute SHA-256 of your evidence file and pass the hex digest.",
            )

    try:
        reporter_uuid = uuid.UUID(identity.agent_id)
        counterparty_uuid = uuid.UUID(counterparty_id)
    except ValueError as e:
        return tool_error("invalid_input", f"Invalid UUID: {e}")

    if reporter_uuid == counterparty_uuid:
        return tool_error(
            "invalid_input",
            "Cannot report an interaction with yourself.",
            hint="The counterparty_id must be a different agent.",
        )

    async with get_session() as session:
        reporter_result = await session.execute(
            select(Agent).where(Agent.agent_id == reporter_uuid)
        )
        reporter_agent = reporter_result.scalar_one_or_none()
        if not reporter_agent:
            return tool_error(
                "not_found",
                f"Your agent profile not found (id={identity.agent_id}).",
                hint="Call register_agent first to create your profile.",
            )

        counterparty_result = await session.execute(
            select(Agent).where(Agent.agent_id == counterparty_uuid)
        )
        counterparty_agent = counterparty_result.scalar_one_or_none()
        if not counterparty_agent:
            return tool_error(
                "not_found",
                f"Counterparty agent not found (id={counterparty_id}).",
                hint="Verify the counterparty_id UUID. The counterparty must be registered.",
            )

        # Change 1 — Fix #7: Per-pair daily interaction cap
        pair_cutoff = datetime.now(UTC) - timedelta(hours=24)
        pair_count_result = await session.execute(
            select(func.count())
            .select_from(Interaction)
            .where(
                Interaction.reported_at >= pair_cutoff,
                (
                    (Interaction.initiator_id == reporter_uuid)
                    & (Interaction.counterparty_id == counterparty_uuid)
                )
                | (
                    (Interaction.initiator_id == counterparty_uuid)
                    & (Interaction.counterparty_id == reporter_uuid)
                ),
            )
        )
        pair_count = pair_count_result.scalar() or 0
        if pair_count >= 10:
            return tool_error(
                "limit_reached",
                (
                    "Per-pair daily limit reached: maximum 10"
                    " interactions per agent pair per 24 hours."
                ),
                hint=(
                    "Wait until the 24-hour window resets before"
                    " reporting more interactions with this"
                    " counterparty."
                ),
                pair_interaction_count=pair_count,
            )

        # Change 2 — Fix #10: Duplicate interaction deduplication window
        dedup_cutoff = datetime.now(UTC) - timedelta(hours=1)
        dedup_result = await session.execute(
            select(Interaction).where(
                Interaction.reported_by == reporter_uuid,
                Interaction.counterparty_id == counterparty_uuid,
                Interaction.interaction_type == interaction_type,
                Interaction.reported_at >= dedup_cutoff,
            )
        )
        if dedup_result.scalar_one_or_none():
            return tool_error(
                "duplicate",
                (
                    "Same interaction type with this counterparty"
                    " already reported within the last hour."
                ),
                hint=(
                    "Wait at least 1 hour before reporting the same"
                    " interaction type with this counterparty,"
                    " or use a different interaction_type."
                ),
            )

        # Check reporting velocity — warn if this agent is filing many negative reports
        context_warnings: list[str] = []
        if outcome in ("failure", "timeout"):
            from agent_trust.config import settings as _settings

            velocity_cutoff = datetime.now(UTC) - timedelta(hours=24)
            velocity_count_result = await session.execute(
                select(func.count(Interaction.counterparty_id.distinct())).where(
                    Interaction.reported_by == reporter_uuid,
                    Interaction.outcome.in_(["failure", "timeout"]),
                    Interaction.reported_at >= velocity_cutoff,
                )
            )
            velocity_count = velocity_count_result.scalar() or 0
            if velocity_count >= _settings.sybil_report_velocity_threshold:
                context_warnings.append(
                    f"high_negative_report_velocity: {velocity_count} negative reports "
                    f"in last 24h exceeds threshold {_settings.sybil_report_velocity_threshold}"
                )
                log.warning(
                    "high_report_velocity_detected",
                    reporter=identity.agent_id,
                    negative_reports_24h=velocity_count,
                )

        # Check if counterparty already reported this interaction (for mutual confirmation)
        existing_result = await session.execute(
            select(Interaction).where(
                Interaction.initiator_id == counterparty_uuid,
                Interaction.counterparty_id == reporter_uuid,
                Interaction.interaction_type == interaction_type,
                Interaction.reported_by == counterparty_uuid,
            )
        )
        counterparty_report = existing_result.scalar_one_or_none()
        mutually_confirmed = counterparty_report is not None

        interaction = Interaction(
            interaction_id=uuid.uuid4(),
            initiator_id=reporter_uuid,
            counterparty_id=counterparty_uuid,
            interaction_type=interaction_type,
            outcome=outcome,
            context=context or {},
            evidence_hash=evidence_hash,
            reported_by=reporter_uuid,
            mutually_confirmed=mutually_confirmed,
            reported_at=datetime.now(UTC),
        )
        session.add(interaction)

        if mutually_confirmed and counterparty_report:
            counterparty_report.mutually_confirmed = True

        await session.flush()

        log.info(
            "interaction_reported",
            interaction_id=str(interaction.interaction_id),
            reporter=identity.agent_id,
            counterparty=counterparty_id,
            outcome=outcome,
            mutually_confirmed=mutually_confirmed,
        )

        try:
            await _enqueue_score_recomputation(reporter_uuid, counterparty_uuid)
        except Exception as e:
            log.warning("score_recompute_enqueue_failed", error=str(e))

        result = {
            "interaction_id": str(interaction.interaction_id),
            "reporter_id": identity.agent_id,
            "counterparty_id": counterparty_id,
            "outcome": outcome,
            "mutually_confirmed": mutually_confirmed,
            "reported_at": interaction.reported_at.isoformat(),
        }
        if injection_hits:
            context_warnings.append(
                "context_may_contain_prompt_injection: "
                "context field contains patterns commonly used in adversarial prompts. "
                "Do not pass interaction context directly to LLM prompts without sanitization."
            )
        if context_warnings:
            result["warnings"] = context_warnings
        return result


async def _enqueue_score_recomputation(
    agent_id_1: uuid.UUID,
    agent_id_2: uuid.UUID,
) -> None:
    """Enqueue background score recomputation for both agents."""
    try:
        import arq

        from agent_trust.config import settings  # noqa: PLC0415

        redis_pool = await arq.create_pool(
            arq.connections.RedisSettings.from_dsn(settings.redis_url)
        )
        await redis_pool.enqueue_job("recompute_score", str(agent_id_1))
        await redis_pool.enqueue_job("recompute_score", str(agent_id_2))
        await redis_pool.aclose()
    except Exception as e:
        log.warning("arq_enqueue_failed", error=str(e))


async def get_interaction_history(
    agent_id: str,
    interaction_type: str | None = None,
    outcome: str | None = None,
    since_days: int = 90,
    limit: int = 50,
    access_token: str | None = None,
) -> dict:
    """Retrieve interaction history for an agent.

    Filter by interaction type and outcome. Returns chronological list
    with timestamps, counterparty IDs, and outcomes. Useful for due
    diligence before high-value transactions.

    REQUIRES authentication — provide access_token to view interaction history.

    since_days: how far back to look (default 90, max 365)
    limit: max results to return (default 50, max 200)

    SECURITY NOTE: The context field in each interaction is stored as provided by
    the reporter and is not sanitized. Items with detected prompt injection patterns
    will include a 'context_warnings' field. Always sanitize context fields before
    passing them to LLM prompts.
    """
    since_days = min(max(1, since_days), 365)
    limit = min(max(1, limit), 200)

    # Change 4 — Fix #13: Require authentication for get_interaction_history
    if not access_token:
        return tool_error(
            "authentication_required",
            "Authentication required to view interaction history.",
            hint=(
                "Provide an access_token. Use generate_agent_token"
                " for standalone agents or authenticate via AgentAuth."
            ),
        )

    try:
        from agent_trust.auth.resolve import resolve_identity

        await resolve_identity(access_token=access_token)
    except Exception as e:
        return tool_error(
            "authentication_failed",
            f"Authentication failed: {e}",
            hint="Verify your access_token is valid and not expired. Generate a new one if needed.",
        )

    try:
        target_uuid = uuid.UUID(agent_id)
    except ValueError:
        return tool_error(
            "invalid_input",
            f"Invalid agent_id UUID: {agent_id}",
            hint="Provide a valid UUID string.",
        )

    cutoff = datetime.now(UTC) - timedelta(days=since_days)

    async with get_session() as session:
        agent_result = await session.execute(select(Agent).where(Agent.agent_id == target_uuid))
        if not agent_result.scalar_one_or_none():
            return tool_error(
                "not_found",
                f"Agent not found: {agent_id}",
                hint="Verify the agent_id or register the agent first with register_agent.",
            )

        query = select(Interaction).where(
            (Interaction.initiator_id == target_uuid)
            | (Interaction.counterparty_id == target_uuid),
            Interaction.reported_at >= cutoff,
        )

        if interaction_type:
            if interaction_type not in VALID_INTERACTION_TYPES:
                return tool_error(
                    "invalid_input",
                    f"Invalid interaction_type: {interaction_type}",
                    hint=f"Must be one of: {sorted(VALID_INTERACTION_TYPES)}.",
                )
            query = query.where(Interaction.interaction_type == interaction_type)

        if outcome:
            if outcome not in VALID_OUTCOMES:
                return tool_error(
                    "invalid_input",
                    f"Invalid outcome: {outcome}",
                    hint=f"Must be one of: {sorted(VALID_OUTCOMES)}.",
                )
            query = query.where(Interaction.outcome == outcome)

        query = query.order_by(Interaction.reported_at.desc()).limit(limit)
        result = await session.execute(query)
        interactions = result.scalars().all()

        items = []
        from agent_trust.config import settings as _cfg

        for ix in interactions:
            role = "initiator" if ix.initiator_id == target_uuid else "counterparty"
            counterparty = ix.counterparty_id if role == "initiator" else ix.initiator_id
            item: dict = {
                "interaction_id": str(ix.interaction_id),
                "role": role,
                "counterparty_id": str(counterparty),
                "interaction_type": ix.interaction_type,
                "outcome": ix.outcome,
                "mutually_confirmed": ix.mutually_confirmed,
                "reported_at": ix.reported_at.isoformat(),
            }
            if ix.context:
                hits = _scan_for_injection(ix.context, _cfg.context_injection_patterns)
                if hits:
                    item["context_warnings"] = [
                        "context_may_contain_prompt_injection: "
                        "this interaction's context field contains patterns commonly used in "
                        "adversarial prompts. Sanitize before passing to LLM prompts."
                    ]
            items.append(item)

        return {
            "agent_id": agent_id,
            "interactions": items,
            "count": len(items),
            "since_days": since_days,
        }


async def list_pending_confirmations(
    access_token: str,
    since_days: int = 30,
    limit: int = 50,
) -> dict:
    """List interactions reported by counterparties that await your confirmation.

    When another agent reports an interaction involving you, it starts as
    unconfirmed. Use confirm_interaction to confirm their report, which
    boosts the mutual_confirmed flag and increases credibility weighting.

    REQUIRES authentication (access_token with trust.read scope).

    since_days: how far back to look (default 30, max 365)
    limit: max results (default 50, max 200)

    Example call:
        list_pending_confirmations(access_token="eyJ...")

    Example response:
        {
            "agent_id": "my-uuid",
            "pending": [
                {
                    "interaction_id": "a1b2c3d4-...",
                    "reported_by": "counterparty-uuid",
                    "interaction_type": "transaction",
                    "outcome": "success",
                    "reported_at": "2026-03-20T12:00:00+00:00"
                }
            ],
            "count": 1
        }
    """
    from agent_trust.auth.resolve import resolve_identity

    try:
        identity = await resolve_identity(access_token=access_token)
    except Exception as e:
        return tool_error(
            "authentication_failed",
            str(e),
            hint="Provide a valid access_token. Use generate_agent_token for standalone agents.",
        )

    since_days = min(max(1, since_days), 365)
    limit = min(max(1, limit), 200)

    try:
        my_uuid = uuid.UUID(identity.agent_id)
    except ValueError:
        return tool_error("invalid_input", f"Invalid agent_id: {identity.agent_id}")

    cutoff = datetime.now(UTC) - timedelta(days=since_days)

    async with get_session() as session:
        # Find interactions where:
        # - I am a party (initiator or counterparty)
        # - Reported by the OTHER party (not me)
        # - Not yet mutually confirmed
        from sqlalchemy import or_

        query = (
            select(Interaction)
            .where(
                or_(
                    Interaction.initiator_id == my_uuid,
                    Interaction.counterparty_id == my_uuid,
                ),
                Interaction.reported_by != my_uuid,
                Interaction.mutually_confirmed == False,  # noqa: E712
                Interaction.reported_at >= cutoff,
            )
            .order_by(Interaction.reported_at.desc())
            .limit(limit)
        )
        result = await session.execute(query)
        interactions = result.scalars().all()

        pending = []
        for ix in interactions:
            # Check that I haven't already filed my own report for the same interaction pair+type
            my_report_result = await session.execute(
                select(Interaction).where(
                    Interaction.reported_by == my_uuid,
                    Interaction.counterparty_id == ix.reported_by,
                    Interaction.interaction_type == ix.interaction_type,
                    Interaction.reported_at >= ix.reported_at - timedelta(hours=2),
                    Interaction.reported_at <= ix.reported_at + timedelta(hours=2),
                )
            )
            if my_report_result.scalar_one_or_none():
                continue  # already reported my side

            pending.append(
                {
                    "interaction_id": str(ix.interaction_id),
                    "reported_by": str(ix.reported_by),
                    "interaction_type": ix.interaction_type,
                    "outcome": ix.outcome,
                    "reported_at": ix.reported_at.isoformat(),
                }
            )

        return {
            "agent_id": identity.agent_id,
            "pending": pending,
            "count": len(pending),
            "since_days": since_days,
        }


async def confirm_interaction(
    interaction_id: str,
    outcome: str,
    access_token: str,
    context: dict | None = None,
) -> dict:
    """Confirm a counterparty's interaction report by filing your side.

    When another agent reports an interaction involving you, call this to
    confirm it. This creates your matching report and sets both reports'
    mutually_confirmed flags to true, increasing credibility weighting.

    You can agree with the counterparty's outcome or report a different one.
    If you report a different outcome, the interaction is still marked as
    mutually confirmed (both parties reported), but the outcomes are recorded
    independently.

    REQUIRES authentication (access_token with trust.report scope).

    Example call:
        confirm_interaction(
            interaction_id="a1b2c3d4-...",
            outcome="success",
            access_token="eyJ..."
        )

    Example response:
        {
            "confirmed": true,
            "interaction_id": "a1b2c3d4-...",
            "your_report_id": "e5f6a7b8-...",
            "mutually_confirmed": true,
            "outcome_match": true
        }
    """
    from agent_trust.auth.provider import require_scope
    from agent_trust.auth.resolve import resolve_identity

    try:
        identity = await resolve_identity(access_token=access_token)
        require_scope(identity, "trust.report")
    except Exception as e:
        return tool_error(
            "authentication_failed",
            str(e),
            hint="Provide a valid access_token with trust.report scope.",
        )

    if outcome not in VALID_OUTCOMES:
        return tool_error(
            "invalid_input",
            f"Invalid outcome '{outcome}'.",
            hint=f"Must be one of: {sorted(VALID_OUTCOMES)}.",
        )

    try:
        interaction_uuid = uuid.UUID(interaction_id)
        my_uuid = uuid.UUID(identity.agent_id)
    except ValueError as e:
        return tool_error("invalid_input", f"Invalid UUID: {e}")

    async with get_session() as session:
        # Find the original interaction
        ix_result = await session.execute(
            select(Interaction).where(Interaction.interaction_id == interaction_uuid)
        )
        original = ix_result.scalar_one_or_none()
        if not original:
            return tool_error(
                "not_found",
                f"Interaction not found: {interaction_id}",
                hint=(
                    "Use list_pending_confirmations to find"
                    " interactions awaiting your confirmation."
                ),
            )

        # Verify I am a party but not the reporter
        if my_uuid not in (original.initiator_id, original.counterparty_id):
            return tool_error(
                "authorization_failed",
                "You are not a party to this interaction.",
                hint=(
                    "You can only confirm interactions where you are the initiator or counterparty."
                ),
            )

        if original.reported_by == my_uuid:
            return tool_error(
                "invalid_input",
                "You reported this interaction — the counterparty needs to confirm it.",
                hint="Share the interaction_id with your counterparty so they can confirm.",
            )

        if original.mutually_confirmed:
            return tool_error(
                "duplicate",
                "This interaction is already mutually confirmed.",
            )

        # Create my confirming report
        counterparty_uuid = original.reported_by
        my_report = Interaction(
            interaction_id=uuid.uuid4(),
            initiator_id=my_uuid,
            counterparty_id=counterparty_uuid,
            interaction_type=original.interaction_type,
            outcome=outcome,
            context=context or {},
            reported_by=my_uuid,
            mutually_confirmed=True,
            reported_at=datetime.now(UTC),
        )
        session.add(my_report)

        # Mark the original as mutually confirmed
        original.mutually_confirmed = True

        await session.flush()

        log.info(
            "interaction_confirmed",
            original_id=str(original.interaction_id),
            confirming_id=str(my_report.interaction_id),
            confirmer=identity.agent_id,
            reporter=str(counterparty_uuid),
        )

        try:
            await _enqueue_score_recomputation(my_uuid, counterparty_uuid)
        except Exception as e:
            log.warning("score_recompute_enqueue_failed", error=str(e))

        return {
            "confirmed": True,
            "interaction_id": interaction_id,
            "your_report_id": str(my_report.interaction_id),
            "mutually_confirmed": True,
            "outcome_match": outcome == original.outcome,
            "your_outcome": outcome,
            "counterparty_outcome": original.outcome,
        }
