#!/usr/bin/env python
"""Register AgentTrust's trust.* scopes with AgentAuth.

Usage:
    uv run python scripts/register_scopes.py

Environment variables required:
    AGENTAUTH_MCP_URL: URL of the AgentAuth MCP server (default: https://agentauth.radi.pro/mcp)
    AGENTAUTH_ACCESS_TOKEN: Bearer token for AgentAuth MCP calls
"""

from __future__ import annotations

import asyncio
import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))


TRUST_SCOPES = [
    {
        "name": "trust.read",
        "description": "Read trust scores and agent profiles",
        "category": "trust",
    },
    {
        "name": "trust.report",
        "description": "Submit interaction reports and rate agents",
        "category": "trust",
    },
    {
        "name": "trust.dispute.file",
        "description": "File a dispute against an interaction outcome",
        "category": "trust",
    },
    {
        "name": "trust.dispute.resolve",
        "description": "Resolve disputes (arbitrator role)",
        "category": "trust",
    },
    {
        "name": "trust.attest.issue",
        "description": "Issue signed trust attestations for agents",
        "category": "trust",
    },
    {
        "name": "trust.admin",
        "description": "Administrative access: subscribe to alerts, manage subscriptions",
        "category": "trust",
    },
]


async def register_scopes() -> None:
    """Register trust.* scopes with AgentAuth MCP server."""
    agentauth_url = os.environ.get("AGENTAUTH_MCP_URL", "https://agentauth.radi.pro/mcp")
    access_token = os.environ.get("AGENTAUTH_ACCESS_TOKEN", "")

    if not access_token:
        print("⚠️  AGENTAUTH_ACCESS_TOKEN not set — skipping AgentAuth registration")
        print("   Set this env var to register scopes with AgentAuth.")
        print()
        print("Trust scopes that would be registered:")
        for scope in TRUST_SCOPES:
            print(f"  - {scope['name']}: {scope['description']}")
        return

    print(f"Connecting to AgentAuth MCP server: {agentauth_url}")

    try:
        from mcp import ClientSession
        from mcp.client.streamable_http import streamablehttp_client
    except ImportError:
        print("❌ MCP client library not available. Install with: uv add mcp")
        sys.exit(1)

    try:
        async with streamablehttp_client(
            agentauth_url,
            headers={"Authorization": f"Bearer {access_token}"},
        ) as (read, write, _):
            async with ClientSession(read, write) as session:
                await session.initialize()
                print("✅ Connected to AgentAuth MCP server")

                tools_result = await session.list_tools()
                tool_names = [t.name for t in tools_result.tools]
                print(f"   Available tools: {', '.join(tool_names[:10])}")

                if "quickstart" in tool_names:
                    print("\nRegistering AgentTrust with AgentAuth...")
                    desc = (
                        "AgentTrust MCP Server — reputation and trust scoring "
                        "for AI agents. Provides trust.* scopes for score "
                        "reporting, attestation, and dispute resolution."
                    )
                    result = await session.call_tool(
                        "quickstart",
                        {
                            "name": "AgentTrust",
                            "agent_type": "service",
                            "description": desc,
                        },
                    )
                    content = result.content[0].text if result.content else "OK"
                    print(f"✅ Quickstart result: {json.dumps(content, indent=2)[:200]}")

                register_tool = next(
                    (t for t in ["register_scope", "create_scope", "add_scope"] if t in tool_names),
                    None,
                )

                if register_tool:
                    n = len(TRUST_SCOPES)
                    print(f"\nRegistering {n} trust.* scopes using '{register_tool}'...")
                    for scope in TRUST_SCOPES:
                        try:
                            result = await session.call_tool(register_tool, scope)
                            status = result.content[0].text if result.content else "OK"
                            print(f"  ✅ {scope['name']}: {str(status)[:80]}")
                        except Exception as e:
                            print(f"  ⚠️  {scope['name']}: {e}")
                else:
                    print("\n⚠️  No scope registration tool found on AgentAuth.")
                    print("   The following scopes should be manually registered:")
                    for scope in TRUST_SCOPES:
                        print(f"  - {scope['name']}: {scope['description']}")

    except Exception as e:
        print(f"❌ Failed to connect to AgentAuth: {e}")
        print("   Scopes not registered. Check AGENTAUTH_MCP_URL and AGENTAUTH_ACCESS_TOKEN.")
        sys.exit(1)

    print("\n✅ Done.")


if __name__ == "__main__":
    asyncio.run(register_scopes())
