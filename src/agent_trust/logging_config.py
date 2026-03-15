from __future__ import annotations

import logging
import sys

import structlog


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

    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(formatter)

    root_logger = logging.getLogger()
    root_logger.handlers = [handler]
    root_logger.setLevel(getattr(logging, log_level.upper(), logging.INFO))


def bind_request_context(request_id: str, tool_name: str | None = None) -> None:
    """Bind correlation context for the current async context.

    Call this at the start of each tool invocation to attach
    request_id and tool_name to all log lines within that scope.
    """
    ctx: dict = {"request_id": request_id}
    if tool_name:
        ctx["tool"] = tool_name
    structlog.contextvars.bind_contextvars(**ctx)


def clear_request_context() -> None:
    """Clear the correlation context. Call at end of tool invocation."""
    structlog.contextvars.clear_contextvars()
