from __future__ import annotations

import argparse
import logging
import sys

import sqlalchemy
import structlog
from mcp.server.fastmcp import FastMCP

from agent_trust.config import settings

structlog.configure(
    processors=[
        structlog.contextvars.merge_contextvars,
        structlog.processors.add_log_level,
        structlog.processors.TimeStamper(fmt="iso"),
        structlog.dev.ConsoleRenderer()
        if sys.stderr.isatty()
        else structlog.processors.JSONRenderer(),  # noqa: E501
    ],
    wrapper_class=structlog.make_filtering_bound_logger(logging.INFO),
    context_class=dict,
    logger_factory=structlog.PrintLoggerFactory(),
)

log = structlog.get_logger()

mcp = FastMCP(
    "AgentTrust",
    instructions=(
        "AgentTrust is a reputation and trust scoring service for AI agents. "
        "Use check_trust to evaluate an agent before a transaction. "
        "Use report_interaction to record outcomes. "
        "Use issue_attestation to get a portable trust certificate. "
        "Use file_dispute to contest incorrect interaction reports."
    ),
)


async def lifespan_startup() -> None:
    """Initialize all shared resources on startup."""
    from agent_trust.db.redis import get_redis
    from agent_trust.db.session import engine

    log.info("agent_trust_startup", transport=settings.mcp_transport)

    try:
        async with engine.begin() as conn:
            await conn.execute(sqlalchemy.text("SELECT 1"))
        log.info("db_connected")
    except Exception as e:
        log.warning("db_connection_failed", error=str(e))

    try:
        redis = await get_redis()
        await redis.ping()
        log.info("redis_connected")
    except Exception as e:
        log.warning("redis_connection_failed", error=str(e))


async def lifespan_shutdown() -> None:
    """Clean up shared resources on shutdown."""
    from agent_trust.db.redis import close_redis
    from agent_trust.db.session import engine

    await engine.dispose()
    await close_redis()
    log.info("agent_trust_shutdown")


def main() -> None:
    """Entry point for the AgentTrust MCP server."""
    parser = argparse.ArgumentParser(description="AgentTrust MCP Server")
    parser.add_argument(
        "--transport",
        choices=["stdio", "streamable-http"],
        default=settings.mcp_transport,
        help="Transport type (default: from MCP_TRANSPORT env var)",
    )
    parser.add_argument(
        "--port",
        type=int,
        default=settings.mcp_port,
        help="Port for HTTP transport (default: from MCP_PORT env var)",
    )
    args = parser.parse_args()

    log.info("starting_agent_trust", transport=args.transport, port=args.port)

    if args.transport == "streamable-http":
        mcp.run(transport="streamable-http", host="0.0.0.0", port=args.port)
    else:
        mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
