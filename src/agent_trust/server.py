from __future__ import annotations

import argparse
import asyncio
import sys

import sqlalchemy
import structlog
import uvicorn
from mcp.server.fastmcp import FastMCP
from mcp.server.transport_security import TransportSecuritySettings
from prometheus_client import make_asgi_app as make_prometheus_asgi_app
from starlette.routing import Mount

from agent_trust.config import settings
from agent_trust.instrument import track_tool_call
from agent_trust.logging_config import configure_logging
from agent_trust.prompts.diagnose import dispute_assessment, explain_score_change
from agent_trust.prompts.evaluate import evaluate_counterparty
from agent_trust.resources.attestations_resource import get_agent_attestations
from agent_trust.resources.disputes_resource import get_dispute
from agent_trust.resources.health import get_health
from agent_trust.resources.history import get_agent_history
from agent_trust.resources.leaderboard import get_leaderboard
from agent_trust.resources.scores import get_agent_score
from agent_trust.tools.agents import (
    generate_agent_token,
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

configure_logging(json_logs=settings.json_logs, log_level=settings.log_level)

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
    # Disable DNS rebinding protection — the server runs behind a reverse proxy
    # (Cloudflare tunnel) which handles host validation at the edge. Enabling this
    # would reject requests where Host != localhost (e.g., host.docker.internal).
    transport_security=TransportSecuritySettings(enable_dns_rebinding_protection=False),
)

# Agent tools
mcp.tool()(track_tool_call(register_agent))
mcp.tool()(track_tool_call(link_agentauth))
mcp.tool()(track_tool_call(generate_agent_token))
mcp.tool()(track_tool_call(whoami))
mcp.tool()(track_tool_call(get_agent_profile))
mcp.tool()(track_tool_call(search_agents))

# Interaction tools
mcp.tool()(track_tool_call(report_interaction))
mcp.tool()(track_tool_call(get_interaction_history))

# Dispute tools
mcp.tool()(track_tool_call(file_dispute))
mcp.tool()(track_tool_call(resolve_dispute))

# Scoring tools
mcp.tool()(track_tool_call(check_trust))
mcp.tool()(track_tool_call(get_score_breakdown))
mcp.tool()(track_tool_call(compare_agents))

# Attestation tools
mcp.tool()(track_tool_call(issue_attestation))
mcp.tool()(track_tool_call(verify_attestation))

# Sybil detection
mcp.tool()(track_tool_call(sybil_check))


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
        host = "0.0.0.0" if settings.environment == "production" else "127.0.0.1"

        if settings.environment == "production":
            # TLS must be terminated by a reverse proxy (e.g. Cloudflare tunnel, nginx,
            # caddy) in front of this server. FastMCP does not support ssl_certfile/
            # ssl_keyfile parameters, so TLS cert paths are intentionally not required.
            log.info(
                "production_mode",
                message="Running in production mode. TLS should be terminated by a reverse proxy.",
            )
        else:
            log.warning(
                "no_tls",
                message="Running HTTP transport without TLS. Not suitable for production.",
            )

        starlette_app = mcp.streamable_http_app()

        if settings.metrics_enabled:
            starlette_app.router.routes.insert(0, Mount("/metrics", app=make_prometheus_asgi_app()))
            log.info("metrics_enabled", path="/metrics", port=args.port)

        uvicorn_config = uvicorn.Config(
            starlette_app,
            host=host,
            port=args.port,
            # Trust proxy headers from Cloudflare tunnel and other reverse proxies.
            # The tunnel forwards requests with Host: host.docker.internal, so we
            # must not restrict allowed hosts at this layer.
            forwarded_allow_ips="*",
            proxy_headers=True,
        )
        uvicorn_server = uvicorn.Server(uvicorn_config)
        asyncio.run(uvicorn_server.serve())
    else:
        mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
