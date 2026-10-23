"""
LeadSync core data models.

Pydantic v2 domain models shared by the API, ingest pipeline, scorer,
queue, and UI. These are the wire/domain objects; `core.database.models`
contains their SQLAlchemy persistence counterparts.
"""

from .conversation import Conversation, ExtractedEntity
from .prospect import ScoredProspect
from .message import FollowUpDraft

__all__ = [
    "Conversation",
    "ExtractedEntity",
    "ScoredProspect",
    "FollowUpDraft",
]
