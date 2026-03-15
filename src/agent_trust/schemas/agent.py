from __future__ import annotations

import uuid
from datetime import datetime

from pydantic import BaseModel, Field


class AgentCreate(BaseModel):
    display_name: str | None = None
    capabilities: list[str] = Field(default_factory=list)
    metadata: dict = Field(default_factory=dict)
    auth_source: str = "agentauth"
    public_key: bytes | None = None


class AgentProfile(BaseModel):
    agent_id: uuid.UUID
    display_name: str | None
    capabilities: list[str]
    auth_source: str
    agentauth_linked: bool
    trust_level: float
    registered_at: datetime
    status: str
