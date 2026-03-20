from __future__ import annotations

from agent_trust.config import settings


async def discover() -> dict:
    """Discover AgentTrust capabilities, tools, auth methods, and rate limits.

    Call this first when connecting to AgentTrust to understand what's
    available and how to authenticate. No authentication required.

    Returns a complete catalog of:
    - Available tools with descriptions and required scopes
    - Supported authentication methods
    - Rate limit tiers by trust level
    - Score types and their meanings
    - Interaction types and outcome options

    Example response (abbreviated):
        {
            "service": "AgentTrust",
            "version": "1.0.0",
            "auth_methods": [...],
            "tools": [...],
            "score_types": {...},
            "rate_limits": {...}
        }
    """
    return {
        "service": "AgentTrust",
        "description": (
            "Reputation and trust scoring service for AI agents. "
            "Evaluate counterparties, report interaction outcomes, "
            "issue portable trust attestations, and detect Sybil attacks."
        ),
        "version": "1.0.0",
        "auth_methods": [
            {
                "method": "agentauth",
                "description": (
                    "AgentAuth bearer token (preferred). Obtain from agentauth.radi.pro."
                ),
                "scopes": [
                    "trust.read",
                    "trust.report",
                    "trust.dispute.file",
                    "trust.dispute.resolve",
                    "trust.attest.issue",
                    "trust.admin",
                ],
            },
            {
                "method": "standalone",
                "description": (
                    "Ed25519 signed JWT. Register with register_agent, "
                    "then generate tokens with generate_agent_token."
                ),
                "scopes": ["trust.read", "trust.report"],
            },
            {
                "method": "auto_keygen",
                "description": (
                    "Call register_agent with no credentials to auto-generate "
                    "an Ed25519 keypair. Store the private_key_hex securely."
                ),
                "scopes": ["trust.read", "trust.report"],
            },
        ],
        "tools": [
            {
                "name": "register_agent",
                "description": "Register a new agent in the trust network",
                "auth_required": False,
                "scopes_required": [],
            },
            {
                "name": "generate_agent_token",
                "description": "Generate a signed access token for standalone agents",
                "auth_required": False,
                "scopes_required": [],
            },
            {
                "name": "whoami",
                "description": "Check your identity and current trust scores",
                "auth_required": True,
                "scopes_required": [],
            },
            {
                "name": "get_agent_profile",
                "description": "Get public profile for any agent",
                "auth_required": False,
                "scopes_required": [],
            },
            {
                "name": "search_agents",
                "description": "Search agents by score, capabilities, and interaction count",
                "auth_required": False,
                "scopes_required": [],
            },
            {
                "name": "check_trust",
                "description": "Check an agent's trust score (primary evaluation tool)",
                "auth_required": False,
                "scopes_required": [],
                "note": "Authenticated calls with trust.read scope get factor_breakdown",
            },
            {
                "name": "check_trust_batch",
                "description": "Check trust scores for multiple agents in one call",
                "auth_required": False,
                "scopes_required": [],
            },
            {
                "name": "get_score_breakdown",
                "description": "Detailed Bayesian factors behind a score",
                "auth_required": True,
                "scopes_required": ["trust.read"],
            },
            {
                "name": "compare_agents",
                "description": "Rank multiple agents by score for selection",
                "auth_required": False,
                "scopes_required": [],
            },
            {
                "name": "report_interaction",
                "description": "Report outcome of an interaction with another agent",
                "auth_required": True,
                "scopes_required": ["trust.report"],
            },
            {
                "name": "get_interaction_history",
                "description": "Retrieve interaction history for an agent",
                "auth_required": True,
                "scopes_required": [],
            },
            {
                "name": "list_pending_confirmations",
                "description": "List interactions awaiting your mutual confirmation",
                "auth_required": True,
                "scopes_required": ["trust.read"],
            },
            {
                "name": "confirm_interaction",
                "description": "Confirm a counterparty's interaction report",
                "auth_required": True,
                "scopes_required": ["trust.report"],
            },
            {
                "name": "file_dispute",
                "description": "Challenge an interaction outcome",
                "auth_required": True,
                "scopes_required": ["trust.dispute.file"],
            },
            {
                "name": "resolve_dispute",
                "description": "Resolve a dispute (arbitrators only)",
                "auth_required": True,
                "scopes_required": ["trust.dispute.resolve"],
            },
            {
                "name": "issue_attestation",
                "description": "Issue signed JWT capturing current trust scores",
                "auth_required": True,
                "scopes_required": ["trust.attest.issue"],
            },
            {
                "name": "verify_attestation",
                "description": "Verify an attestation JWT signature and status",
                "auth_required": False,
                "scopes_required": [],
            },
            {
                "name": "sybil_check",
                "description": "Run Sybil detection checks against an agent",
                "auth_required": False,
                "scopes_required": [],
            },
            {
                "name": "link_agentauth",
                "description": "Link standalone profile to AgentAuth identity",
                "auth_required": True,
                "scopes_required": [],
            },
            {
                "name": "subscribe_alerts",
                "description": "Subscribe to trust score change notifications",
                "auth_required": True,
                "scopes_required": ["trust.admin"],
            },
        ],
        "score_types": {
            "overall": "Composite score across all interaction types",
            "reliability": "Based on transaction, delegation, and collaboration outcomes",
            "responsiveness": "Based on query and delegation timeliness",
            "honesty": "Based on collaboration outcomes",
        },
        "interaction_types": {
            "transaction": "Value exchange between agents",
            "delegation": "Task delegation from one agent to another",
            "query": "Information request and response",
            "collaboration": "Joint work on a shared task",
        },
        "outcomes": {
            "success": "Interaction completed as expected",
            "failure": "Interaction failed or was not fulfilled",
            "timeout": "Interaction timed out without completion",
            "partial": "Interaction partially completed",
        },
        "rate_limits": {
            "window_seconds": 60,
            "base_requests_per_minute": settings.rate_limit_base,
            "tiers": {
                "root": int(settings.rate_limit_base * settings.rate_limit_root_multiplier),
                "delegated": int(
                    settings.rate_limit_base * settings.rate_limit_delegated_multiplier
                ),
                "standalone": int(
                    settings.rate_limit_base * settings.rate_limit_standalone_multiplier
                ),
                "ephemeral": int(
                    settings.rate_limit_base * settings.rate_limit_ephemeral_multiplier
                ),
                "unauthenticated": settings.rate_limit_unauthenticated,
            },
        },
        "quickstart": [
            "1. Call discover() to understand capabilities (you are here)",
            "2. Call register_agent() to create your identity",
            "3. Call generate_agent_token(agent_id, private_key_hex) to get an access_token",
            "4. Call check_trust(agent_id) to evaluate counterparties before transacting",
            "5. Call report_interaction(...) after completing interactions",
            "6. Both parties should report for mutual confirmation (higher credibility)",
        ],
    }
