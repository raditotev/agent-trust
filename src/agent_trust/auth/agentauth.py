from __future__ import annotations

import json

import structlog
from mcp import ClientSession
from mcp.client.streamable_http import streamablehttp_client

from agent_trust.auth.cache import cached_introspect
from agent_trust.auth.identity import AgentIdentity, AuthenticationError
from agent_trust.config import settings

log = structlog.get_logger()


class AgentAuthProvider:
    """Authentication provider using AgentAuth MCP server.

    Connects to AgentAuth's MCP server as an MCP client and calls
    introspect_token and check_permission tools to verify agent identity.
    """

    def __init__(self, redis_client=None) -> None:
        self._redis = redis_client

    async def _call_agentauth_tool(self, tool_name: str, arguments: dict) -> dict:
        """Call an AgentAuth MCP tool and return the result."""
        headers = {}
        if settings.agentauth_access_token:
            headers["Authorization"] = f"Bearer {settings.agentauth_access_token}"

        async with streamablehttp_client(settings.agentauth_mcp_url, headers=headers) as (
            read,
            write,
            _,
        ):
            async with ClientSession(read, write) as session:
                await session.initialize()
                result = await session.call_tool(tool_name, arguments)
                if result.content and len(result.content) > 0:
                    content = result.content[0]
                    if hasattr(content, "text"):
                        return json.loads(content.text)
                return {}

    async def _introspect_token_raw(self, access_token: str) -> dict:
        """Call AgentAuth introspect_token MCP tool."""
        try:
            return await self._call_agentauth_tool("introspect_token", {"token": access_token})
        except Exception as e:
            log.warning("agentauth_introspect_failed", error=str(e))
            return {"active": False}

    async def authenticate(
        self,
        access_token: str | None = None,
        public_key_hex: str | None = None,
    ) -> AgentIdentity:
        """Authenticate via AgentAuth token introspection."""
        if not access_token:
            raise AuthenticationError("AgentAuth provider requires access_token")

        if self._redis:
            introspection = await cached_introspect(
                access_token,
                self._introspect_token_raw,
                self._redis,
            )
        else:
            introspection = await self._introspect_token_raw(access_token)

        if not introspection.get("active"):
            raise AuthenticationError("Invalid or expired AgentAuth token")

        agent_id = introspection.get("sub") or introspection.get("agent_id")
        if not agent_id:
            raise AuthenticationError("Token missing subject (agent_id)")

        scopes_raw = introspection.get("scopes", introspection.get("scope", ""))
        if isinstance(scopes_raw, str):
            scopes = scopes_raw.split() if scopes_raw else []
        else:
            scopes = list(scopes_raw)

        return AgentIdentity(
            agent_id=str(agent_id),
            source="agentauth",
            scopes=scopes,
            trust_level=introspection.get("trust_level", "ephemeral"),
        )

    async def check_permission(
        self,
        identity: AgentIdentity,
        action: str,
        resource: str,
    ) -> bool:
        """Check permission via AgentAuth check_permission MCP tool."""
        try:
            result = await self._call_agentauth_tool(
                "check_permission",
                {
                    "agent_id": identity.agent_id,
                    "action": action,
                    "resource": resource,
                    "access_token": settings.agentauth_access_token,
                },
            )
            return result.get("allowed", False)
        except Exception as e:
            log.warning("agentauth_check_permission_failed", error=str(e))
            return False
