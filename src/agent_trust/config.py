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
    signing_key_password: str = (
        ""  # Password for encrypting Ed25519 signing key (empty = unencrypted for dev)
    )

    # Environment
    environment: str = "development"  # "development" or "production"

    # TLS (for streamable-http transport)
    tls_cert_path: str = ""  # Path to TLS certificate PEM file
    tls_key_path: str = ""  # Path to TLS private key PEM file

    # Scoring parameters
    score_half_life_days: float = 90.0
    dispute_penalty: float = 0.03
    attestation_ttl_hours: int = 24

    # Transport
    mcp_transport: Literal["stdio", "streamable-http"] = "stdio"
    mcp_port: int = 8000

    # Logging
    log_level: str = "INFO"
    json_logs: bool = False  # set True in production

    # Rate limiting (requests per minute per agent)
    rate_limit_base: int = 60
    rate_limit_root_multiplier: float = 5.0
    rate_limit_delegated_multiplier: float = 2.0
    rate_limit_standalone_multiplier: float = 1.0
    rate_limit_ephemeral_multiplier: float = 0.5
    rate_limit_unauthenticated: int = 10


settings = Settings()
