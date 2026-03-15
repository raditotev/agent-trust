from __future__ import annotations

import uuid
from datetime import datetime

from pydantic import BaseModel


class DisputeCreate(BaseModel):
    interaction_id: uuid.UUID
    reason: str
    evidence: dict = {}


class DisputeResult(BaseModel):
    dispute_id: uuid.UUID
    interaction_id: uuid.UUID
    filed_by: uuid.UUID
    filed_against: uuid.UUID
    reason: str
    status: str
    resolution: str | None = None
    created_at: datetime
