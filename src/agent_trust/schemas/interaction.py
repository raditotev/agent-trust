from __future__ import annotations

import uuid
from datetime import datetime

from pydantic import BaseModel


class InteractionCreate(BaseModel):
    counterparty_id: uuid.UUID
    interaction_type: str
    outcome: str
    context: dict = {}
    evidence_hash: str | None = None


class InteractionResult(BaseModel):
    interaction_id: uuid.UUID
    initiator_id: uuid.UUID
    counterparty_id: uuid.UUID
    interaction_type: str
    outcome: str
    mutually_confirmed: bool
    reported_at: datetime
