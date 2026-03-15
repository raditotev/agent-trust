from __future__ import annotations

import structlog

from agent_trust.logging_config import (
    bind_request_context,
    clear_request_context,
    configure_logging,
)


def test_configure_logging_dev_mode():
    """configure_logging runs without error in dev mode."""
    configure_logging(json_logs=False, log_level="DEBUG")
    log = structlog.get_logger("test")
    # Should not raise
    log.info("test_event", key="value")


def test_configure_logging_json_mode():
    """configure_logging runs without error in JSON mode."""
    configure_logging(json_logs=True, log_level="INFO")
    log = structlog.get_logger("test")
    log.info("test_json_event", key="value")


def test_bind_and_clear_context():
    """bind_request_context attaches context; clear removes it."""
    configure_logging(json_logs=False, log_level="INFO")
    bind_request_context("req-123", tool_name="check_trust")
    ctx = structlog.contextvars.get_contextvars()
    assert ctx.get("request_id") == "req-123"
    assert ctx.get("tool") == "check_trust"

    clear_request_context()
    ctx_after = structlog.contextvars.get_contextvars()
    assert "request_id" not in ctx_after
