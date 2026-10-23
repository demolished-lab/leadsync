"""
FollowUpDraft domain model.

Holds the three tone variants of a generated follow-up plus sending and
engagement state. Mirrors `core.database.models.FollowUpDraftRecord`
for persistence.
"""

from datetime import datetime
from typing import Optional

from pydantic import BaseModel, Field

VARIANT_KEYS = ("variant_agreeable", "variant_direct", "variant_soft")


class FollowUpDraft(BaseModel):
    """A generated follow-up draft with its three tone variants."""

    prospect_id: str
    conversation_id: str

    # Draft variants
    variant_agreeable: str = ""
    variant_direct: str = ""
    variant_soft: str = ""
    selected_variant: Optional[str] = None
    # tone used to generate (agreeable|direct|soft), None = rep hasn't chosen

    # Sending state
    sent: bool = False
    sent_at: Optional[datetime] = None
    send_attempts: int = 0
    send_status: Optional[str] = None  # sent|failed|suppressed
    to_address: Optional[str] = None
    error_message: Optional[str] = None

    # Engagement tracking
    opens: int = 0
    clicks: int = 0
    replies: int = 0

    created_at: datetime = Field(default_factory=lambda: datetime.utcnow())

    def variant_for_urgency(self, urgency: str) -> str:
        """Pick the variant text matching a prospect's urgency band."""
        key = {
            "high": "variant_direct",
            "medium": "variant_agreeable",
            "low": "variant_soft",
        }.get(urgency, "variant_agreeable")
        return getattr(self, key) or ""

    def next_variant_on_retry(self) -> str:
        """Alternate tone on repeated send attempts."""
        cycle = ("variant_direct", "variant_agreeable", "variant_soft")
        idx = self.send_attempts % len(cycle)
        return getattr(self, cycle[idx]) or ""
