from __future__ import annotations

import logging
import sys
from typing import Any

import structlog


def _add_service_name(
    logger: Any,  # noqa: ANN401
    method_name: str,
    event_dict: dict[str, Any],
) -> dict[str, Any]:
    """Stamp every log line with the service label the shared Loki/Alloy
    pipeline lifts into the `service` label (~/Projects/observability).
    Without this, {service="agent-trust"} matches nothing."""
    event_dict.setdefault("service_name", "agent-trust")
    return event_dict


def configure_logging(json_logs: bool = False, log_level: str = "INFO") -> None:
    """Configure structlog for the AgentTrust MCP server.

    In production (json_logs=True): outputs JSON with timestamp, level, logger, event.
    In development (json_logs=False): outputs colored console output.

    Call this once at server startup before any logging occurs.
    """
    shared_processors: list = [
        structlog.contextvars.merge_contextvars,
        structlog.stdlib.add_logger_name,
        structlog.stdlib.add_log_level,
        structlog.processors.TimeStamper(fmt="iso"),
        structlog.processors.StackInfoRenderer(),
        _add_service_name,
    ]

    if json_logs:
        renderer = structlog.processors.JSONRenderer()
    else:
        renderer = structlog.dev.ConsoleRenderer(colors=True)

    structlog.configure(
        processors=[
            *shared_processors,
            structlog.stdlib.ProcessorFormatter.wrap_for_formatter,
        ],
        logger_factory=structlog.stdlib.LoggerFactory(),
        wrapper_class=structlog.stdlib.BoundLogger,
        cache_logger_on_first_use=True,
    )

    formatter = structlog.stdlib.ProcessorFormatter(
        processor=renderer,
        foreign_pre_chain=shared_processors,
    )

    # stderr, never stdout. On the stdio transport (the default) stdout IS the
    # JSON-RPC channel — a log line written there is a protocol frame the
    # client can't parse, and the MCP spec forbids it. Nothing is lost under
    # streamable-http either: Docker and Loki collect both streams.
    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(formatter)

    root_logger = logging.getLogger()
    root_logger.handlers = [handler]
    root_logger.setLevel(getattr(logging, log_level.upper(), logging.INFO))


def bind_request_context(
    request_id: str, tool_name: str | None = None, authenticated: bool | None = None
) -> None:
    """Bind correlation context for the current async context.

    Call this at the start of each tool invocation to attach request_id,
    tool_name, and (when known) whether the call carried credentials to all
    log lines within that scope. `authenticated` is not the caller's identity
    — most tool signatures use `agent_id` for the *subject* being queried
    (e.g. check_trust's counterparty), not the caller — just whether an
    access_token/public_key_hex argument was present.
    """
    ctx: dict = {"request_id": request_id}
    if tool_name:
        ctx["tool"] = tool_name
    if authenticated is not None:
        ctx["authenticated"] = authenticated
    structlog.contextvars.bind_contextvars(**ctx)


def clear_request_context() -> None:
    """Clear the correlation context. Call at end of tool invocation."""
    structlog.contextvars.clear_contextvars()
