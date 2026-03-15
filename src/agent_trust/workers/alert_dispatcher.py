from __future__ import annotations

import structlog

log = structlog.get_logger()


async def dispatch_alerts(ctx: dict, agent_id: str, old_score: float, new_score: float) -> dict:
    """arq worker task: Dispatch alerts when an agent's score changes significantly.

    Compares old vs new score against each subscriber's threshold_delta.
    If the change exceeds the threshold, queues a callback notification.

    Full implementation in Task 18.
    """
    delta = abs(new_score - old_score)
    log.info("alert_check", agent_id=agent_id, delta=delta)
    # Stub: full implementation in Task 18
    return {"agent_id": agent_id, "delta": delta, "alerts_dispatched": 0}
