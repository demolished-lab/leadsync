"""
Conversation domain model plus the entity payload extracted from it.

A `Conversation` is the normalized representation of one inbound touchpoint
(email, call transcript, or meeting) after ingest. The scorer takes these;
the queue stores them inside `ScoredProspect`.
"""

import uuid
from datetime import datetime
from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field, field_validator

VALID_SOURCES = ("email", "call", "meeting")
VALID_URGENCIES = ("high", "medium", "low")


class ExtractedEntity(BaseModel):
    """Entities/signals extracted from raw conversation text."""

    name: Optional[str] = None
    company: Optional[str] = None
    sentiment: Optional[str] = None  # positive|negative|neutral
    sentiment_score: float = 0.0  # 0.0 (negative) .. 1.0 (positive)
    commitment: Optional[str] = None
    pain_point: Optional[str] = None

    @field_validator("sentiment_score")
    @classmethod
    def _validate_sentiment_score(cls, v: float) -> float:
        if v is None:
            return 0.0
        if v < -1.0 or v > 1.0:
            raise ValueError("sentiment_score must be between -1.0 and 1.0")
        return v


class Conversation(BaseModel):
    """One normalized inbound conversation (email / call / meeting)."""

    id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    source: str  # email|call|meeting — validated below
    participants: List[Dict[str, Any]] = Field(default_factory=list)
    date: datetime = Field(default_factory=lambda: datetime.utcnow())
    raw_text: str
    commitments: List[str] = Field(default_factory=list)
    entities: ExtractedEntity = Field(default_factory=ExtractedEntity)
    sentiment: str = "neutral"
    urgency: str = "low"
    deal_size: Optional[float] = None
    metadata: Dict[str, Any] = Field(default_factory=dict)

    @field_validator("source")
    @classmethod
    def _validate_source(cls, v: str) -> str:
        if v not in VALID_SOURCES:
            raise ValueError(
                f"source must be one of {VALID_SOURCES}, got {v!r}"
            )
        return v

    @field_validator("urgency")
    @classmethod
    def _validate_urgency(cls, v: str) -> str:
        if v not in VALID_URGENCIES:
            raise ValueError(
                f"urgency must be one of {VALID_URGENCIES}, got {v!r}"
            )
        return v

    @field_validator("raw_text")
    @classmethod
    def _validate_raw_text(cls, v: str) -> str:
        if not v or not v.strip():
            raise ValueError("raw_text must be non-empty")
        return v

    @field_validator("participants")
    @classmethod
    def _validate_participants(
        cls, v: Optional[List[Dict[str, Any]]]
    ) -> List[Dict[str, Any]]:
        return v or []

    @field_validator("commitments")
    @classmethod
    def _validate_commitments(
        cls, v: Optional[List[str]]
    ) -> List[str]:
        return v or []

    @property
    def primary_name(self) -> str:
        """Best-effort display name of the primary participant."""
        if self.participants:
            return (
                self.participants[0].get("name")
                or self.participants[0].get("email")
                or "Unknown"
            )
        return self.entities.name or "Unknown"

    @property
    def primary_email(self) -> str:
        """Best-effort email of the primary participant."""
        for p in self.participants:
            email = p.get("email")
            if email and "@" in email:
                return email
        return ""
