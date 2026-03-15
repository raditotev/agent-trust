from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import Boolean, CheckConstraint, ForeignKey, Index, String, Text
from sqlalchemy.dialects.postgresql import JSONB, TIMESTAMP, UUID
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.sql import func

from agent_trust.models import Base


class Interaction(Base):
    __tablename__ = "interactions"

    interaction_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    initiator_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("agents.agent_id"), nullable=False)
    counterparty_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("agents.agent_id"), nullable=False)
    interaction_type: Mapped[str] = mapped_column(String(50), nullable=False)
    outcome: Mapped[str] = mapped_column(String(20), nullable=False)
    context: Mapped[dict] = mapped_column(JSONB, server_default="{}")
    evidence_hash: Mapped[str | None] = mapped_column(Text, nullable=True)
    reported_by: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("agents.agent_id"), nullable=False)
    mutually_confirmed: Mapped[bool] = mapped_column(Boolean, server_default="false")
    reported_at: Mapped[datetime] = mapped_column(TIMESTAMP(timezone=True), nullable=False, server_default=func.now())

    __table_args__ = (
        CheckConstraint("initiator_id != counterparty_id", name="no_self_report"),
        Index("idx_interactions_initiator", "initiator_id", "reported_at"),
        Index("idx_interactions_counterparty", "counterparty_id", "reported_at"),
    )
