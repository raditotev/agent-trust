from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import Boolean, Index, String, Text
from sqlalchemy.dialects.postgresql import ARRAY, BYTEA, JSONB, TIMESTAMP, UUID
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.sql import func

from agent_trust.models import Base


class Agent(Base):
    __tablename__ = "agents"

    agent_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    display_name: Mapped[str | None] = mapped_column(Text, nullable=True)
    capabilities: Mapped[list[str]] = mapped_column(ARRAY(String), server_default="{}")
    metadata_: Mapped[dict] = mapped_column("metadata", JSONB, server_default="{}")
    trust_level: Mapped[float] = mapped_column(server_default="0.5000")

    auth_source: Mapped[str] = mapped_column(String(20), nullable=False, server_default="agentauth")
    public_key: Mapped[bytes | None] = mapped_column(BYTEA, nullable=True)
    agentauth_linked: Mapped[bool] = mapped_column(Boolean, server_default="false")

    registered_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), server_default=func.now()
    )
    status: Mapped[str] = mapped_column(String(20), server_default="active")
    delegated_by: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)

    __table_args__ = (
        Index("idx_agents_public_key", "public_key", postgresql_where="public_key IS NOT NULL"),
        Index("idx_agents_capabilities", "capabilities", postgresql_using="gin"),
        Index("idx_agents_auth_source", "auth_source"),
    )
