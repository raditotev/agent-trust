from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import ForeignKey, Integer, Numeric, String
from sqlalchemy.dialects.postgresql import JSONB, TIMESTAMP, UUID
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.sql import func

from agent_trust.models import Base


class TrustScore(Base):
    __tablename__ = "trust_scores"

    agent_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("agents.agent_id"), primary_key=True
    )
    score_type: Mapped[str] = mapped_column(String(50), primary_key=True)
    score: Mapped[float] = mapped_column(Numeric(5, 4), nullable=False)
    confidence: Mapped[float] = mapped_column(Numeric(5, 4), nullable=False)
    interaction_count: Mapped[int] = mapped_column(Integer, nullable=False)
    factor_breakdown: Mapped[dict] = mapped_column(JSONB, server_default="{}")
    computed_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), server_default=func.now()
    )
