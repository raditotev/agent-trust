from __future__ import annotations

import uuid
from datetime import datetime

from pydantic import BaseModel


class TrustScoreResult(BaseModel):
    agent_id: uuid.UUID
    score_type: str
    score: float
    confidence: float
    interaction_count: int
    factor_breakdown: dict = {}
    computed_at: datetime


class ScoreBreakdown(BaseModel):
    agent_id: uuid.UUID
    overall: TrustScoreResult | None = None
    bayesian_raw: float
    dispute_penalty: float
    interactions_weighted: int
    lost_disputes: int
