from __future__ import annotations

import argparse
import logging
import sys

import sqlalchemy
import structlog
from mcp.server.fastmcp import FastMCP

from agent_trust.config import settings
from agent_trust.prompts.diagnose import dispute_assessment, explain_score_change
from agent_trust.prompts.evaluate import evaluate_counterparty
from agent_trust.resources.attestations_resource import get_agent_attestations
from agent_trust.resources.disputes_resource import get_dispute
from agent_trust.resources.health import get_health
from agent_trust.resources.history import get_agent_history
from agent_trust.resources.leaderboard import get_leaderboard
from agent_trust.resources.scores import get_agent_score
from agent_trust.tools.agents import (
    get_agent_profile,
    link_agentauth,
    register_agent,
    search_agents,
    whoami,
)
from agent_trust.tools.attestations import issue_attestation, verify_attestation
from agent_trust.tools.disputes import file_dispute, resolve_dispute
from agent_trust.tools.interactions import get_interaction_history, report_interaction
from agent_trust.tools.scoring import check_trust, compare_agents, get_score_breakdown
from agent_trust.tools.sybil import sybil_check

structlog.configure(
    processors=[
        structlog.contextvars.merge_contextvars,
        structlog.processors.add_log_level,
        structlog.processors.TimeStamper(fmt="iso"),
        structlog.dev.ConsoleRenderer()
        if sys.stderr.isatty()
        else structlog.processors.JSONRenderer(),
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

# Agent tools
mcp.tool()(register_agent)
mcp.tool()(link_agentauth)
mcp.tool()(whoami)
mcp.tool()(get_agent_profile)
mcp.tool()(search_agents)

# Interaction tools
mcp.tool()(report_interaction)
mcp.tool()(get_interaction_history)

# Dispute tools
mcp.tool()(file_dispute)
mcp.tool()(resolve_dispute)

# Scoring tools
mcp.tool()(check_trust)
mcp.tool()(get_score_breakdown)
mcp.tool()(compare_agents)

# Attestation tools
mcp.tool()(issue_attestation)
mcp.tool()(verify_attestation)

# Sybil detection
mcp.tool()(sybil_check)


# Prompts
@mcp.prompt()
def evaluate_counterparty_prompt(
    agent_id: str,
    transaction_value: str = "unknown",
    transaction_type: str = "general",
) -> str:
    """Structured evaluation of a potential counterparty agent."""
    return evaluate_counterparty(agent_id, transaction_value, transaction_type)


@mcp.prompt()
def explain_score_change_prompt(agent_id: str) -> str:
    """Diagnostic investigation of a trust score change."""
    return explain_score_change(agent_id)


@mcp.prompt()
def dispute_assessment_prompt(dispute_id: str) -> str:
    """Structured arbitrator assessment for dispute resolution."""
    return dispute_assessment(dispute_id)


# Resources
@mcp.resource("trust://agents/{agent_id}/score")
async def agent_score_resource(agent_id: str) -> str:
    """Current trust scores for an agent in all categories."""
    return await get_agent_score(agent_id)


@mcp.resource("trust://agents/{agent_id}/history")
async def agent_history_resource(agent_id: str) -> str:
    """Recent interaction history summary (last 90 days)."""
    return await get_agent_history(agent_id)


@mcp.resource("trust://agents/{agent_id}/attestations")
async def agent_attestations_resource(agent_id: str) -> str:
    """Active (non-expired, non-revoked) attestations for an agent."""
    return await get_agent_attestations(agent_id)


@mcp.resource("trust://leaderboard/{score_type}")
async def leaderboard_resource(score_type: str) -> str:
    """Top 50 agents ranked by the specified score type."""
    return await get_leaderboard(score_type)


@mcp.resource("trust://disputes/{dispute_id}")
async def dispute_resource(dispute_id: str) -> str:
    """Full details of a specific dispute."""
    return await get_dispute(dispute_id)


@mcp.resource("trust://health")
async def health_resource() -> str:
    """Service health: DB, Redis, AgentAuth MCP reachability, worker queue."""
    return await get_health()


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
