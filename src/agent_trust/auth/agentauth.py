from __future__ import annotations

import asyncio
import json

import structlog
from mcp import ClientSession
from mcp.client.streamable_http import streamablehttp_client

from agent_trust.auth.cache import cached_introspect
from agent_trust.auth.identity import AgentIdentity, AuthenticationError
from agent_trust.config import settings

log = structlog.get_logger()

# Module-level persistent connection state
_session_lock = asyncio.Lock()
_persistent_session: ClientSession | None = None
_persistent_streams: tuple | None = None  # (read, write, get_url) context managers
_persistent_context: object | None = None  # the async context manager itself


async def _get_persistent_session() -> ClientSession:
    """Get or create a persistent MCP client session to AgentAuth.

    Reuses a single connection across calls to avoid TCP+TLS handshake per request.
    Falls back to a fresh connection if the persistent session is broken.
    """
    global _persistent_session, _persistent_streams, _persistent_context

    async with _session_lock:
        if _persistent_session is not None:
            try:
                # Quick health check — list tools is lightweight
                await _persistent_session.list_tools()
                return _persistent_session
            except Exception:
                log.info("agentauth_session_stale", action="reconnecting")
                await _close_persistent_session_unlocked()

        headers = {}
        if settings.agentauth_access_token:
            headers["Authorization"] = f"Bearer {settings.agentauth_access_token}"

        ctx = streamablehttp_client(settings.agentauth_mcp_url, headers=headers)
        read, write, _ = await ctx.__aenter__()
        _persistent_context = ctx

        session = ClientSession(read, write)
        await session.__aenter__()
        await session.initialize()
        _persistent_session = session
        log.info("agentauth_session_established")
        return _persistent_session


async def _close_persistent_session_unlocked() -> None:
    """Close the persistent session (caller must hold _session_lock)."""
    global _persistent_session, _persistent_context
    if _persistent_session is not None:
        try:
            await _persistent_session.__aexit__(None, None, None)
        except Exception:
            pass
        _persistent_session = None
    if _persistent_context is not None:
        try:
            await _persistent_context.__aexit__(None, None, None)
        except Exception:
            pass
        _persistent_context = None


async def close_agentauth_session() -> None:
    """Close the persistent AgentAuth session (call during shutdown)."""
    async with _session_lock:
        await _close_persistent_session_unlocked()


class AgentAuthProvider:
    """Authentication provider using AgentAuth MCP server.

    Connects to AgentAuth's MCP server as an MCP client and calls
    introspect_token and check_permission tools to verify agent identity.

    Uses a persistent connection pool to avoid per-call TCP+TLS overhead.
    Falls back to single-use connections if the pool is unavailable.
    """

    def __init__(self, redis_client=None) -> None:
        self._redis = redis_client

    async def _call_agentauth_tool(self, tool_name: str, arguments: dict) -> dict:
        """Call an AgentAuth MCP tool using the persistent session.

        Falls back to a fresh single-use connection on persistent session failure.
        """
        # Try persistent session first
        try:
            session = await _get_persistent_session()
            result = await session.call_tool(tool_name, arguments)
            if result.content and len(result.content) > 0:
                content = result.content[0]
                if hasattr(content, "text"):
                    return json.loads(content.text)
            return {}
        except Exception as e:
            log.warning(
                "agentauth_persistent_call_failed",
                tool=tool_name,
                error=str(e),
                action="fallback_to_single_use",
            )

        # Fallback: single-use connection
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
