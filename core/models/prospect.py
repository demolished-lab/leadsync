"""
ScoredProspect domain model.

A `ScoredProspect` wraps a `Conversation` with the scorer's output:
priority score, SLA deadline, and queue state. It is the unit stored in
both the in-memory and Redis priority queues.
"""

from datetime import datetime
from typing import Optional

from pydantic import BaseModel, Field

from .conversation import Conversation


class ScoredProspect(BaseModel):
    """A scored conversation ready for the follow-up queue."""

    conversation_id: str
    priority_score: float = Field(ge=0.0, le=1.0)
    conversation: Conversation

    # Scoring breakdown (audit trail of *why* the score is what it is)
    recency_days: float = 0.0
    engagement_probability: float = 0.0
    deal_value_normalized: float = 0.0
    urgency_score: float = 0.0

    # SLA
    sla_deadline: Optional[datetime] = None
    sla_breached: bool = False
    times_requeued: int = 0

    # Queue state: queued|breached|followed_up|closed
    status: str = "queued"
    response_status: Optional[str] = None  # replied|opened|clicked|none
    last_action_at: Optional[datetime] = None

    def mark_action(self, action: str) -> None:
        """Record that an action was taken on this prospect."""
        self.last_action_at = datetime.utcnow()
        if action in ("followed_up", "closed", "breached", "queued"):
            self.status = action
