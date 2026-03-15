from __future__ import annotations

from sqlalchemy.orm import DeclarativeBase


class Base(DeclarativeBase):
    pass


from agent_trust.models.agent import Agent  # noqa: E402
from agent_trust.models.alert_subscription import AlertSubscription  # noqa: E402
from agent_trust.models.attestation import Attestation  # noqa: E402
from agent_trust.models.dispute import Dispute  # noqa: E402
from agent_trust.models.interaction import Interaction  # noqa: E402
from agent_trust.models.trust_score import TrustScore  # noqa: E402

__all__ = [
    "Base",
    "Agent",
    "Interaction",
    "TrustScore",
    "Dispute",
    "Attestation",
    "AlertSubscription",
]
