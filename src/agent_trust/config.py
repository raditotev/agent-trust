from __future__ import annotations

from typing import Literal
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8")

    # Database
    database_url: str = "postgresql+asyncpg://postgres:postgres@localhost:5432/agent_trust"

    # Redis
    redis_url: str = "redis://localhost:6379/0"

    # Authentication
    auth_provider: Literal["agentauth", "standalone", "both"] = "both"

    # AgentAuth MCP integration
    agentauth_mcp_url: str = "https://agentauth.radi.pro/mcp"
    agentauth_access_token: str = ""

    # Signing key
    signing_key_path: str = "keys/service.key"

    # Scoring parameters
    score_half_life_days: float = 90.0
    dispute_penalty: float = 0.03
    attestation_ttl_hours: int = 24

    # Transport
    mcp_transport: Literal["stdio", "streamable-http"] = "stdio"
    mcp_port: int = 8000


settings = Settings()
