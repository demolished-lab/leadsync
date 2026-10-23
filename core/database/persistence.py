"""
Best-effort persistence of domain objects into the relational store.

The ingest/score/queue pipeline is in-memory-first (low latency), but every
conversation that enters the pipeline should also land in the database so
that history, exports and audit survive restarts. All functions here are
failure-tolerant: if the database is unavailable they log and return False
instead of breaking the request path.
"""

import logging
from typing import Optional

logger = logging.getLogger("Persistence")


def persist_conversation(conv) -> bool:
    """Store a Conversation as a ConversationRecord. Best-effort."""
    try:
        from . import get_db
        from .models import ConversationRecord

        with get_db() as db:
            exists = db.query(ConversationRecord).filter(ConversationRecord.id == conv.id).first()
            if exists:
                return True  # already stored (idempotent)
            record = ConversationRecord(
                id=conv.id,
                source=conv.source,
                participants=conv.participants or {},
                raw_text=conv.raw_text,
                commitments=conv.commitments or [],
                entities=conv.entities.model_dump() if conv.entities is not None else {},
                sentiment=conv.sentiment,
                deal_size=conv.deal_size,
                urgency=conv.urgency,
                conversation_date=conv.date,
                tags=[],
            )
            db.add(record)
        try:
            from .audit import audit
            audit.log(
                "conversation:stored",
                entity_type="conversation",
                entity_id=conv.id,
                details={"source": conv.source, "urgency": conv.urgency},
            )
        except Exception:
            pass
        return True
    except Exception as e:
        logger.debug(f"persist_conversation skipped for {getattr(conv, 'id', '?')}: {e}")
        return False


def persist_scored_prospect(scored) -> bool:
    """Store a ScoredProspect as a ScoredProspectRecord. Best-effort."""
    try:
        from . import get_db
        from .models import ScoredProspectRecord

        with get_db() as db:
            record = ScoredProspectRecord(
                conversation_id=scored.conversation_id,
                priority_score=scored.priority_score,
                recency_days=scored.recency_days,
                engagement_probability=scored.engagement_probability,
                deal_value_normalized=scored.deal_value_normalized,
                urgency_score=scored.urgency_score,
                sla_deadline=scored.sla_deadline,
                sla_breached=scored.sla_breached,
                times_requeued=scored.times_requeued,
                status=scored.status,
            )
            db.add(record)
        return True
    except Exception as e:
        logger.debug(f"persist_scored_prospect skipped for {getattr(scored, 'conversation_id', '?')}: {e}")
        return False


def persist_followup_draft(
    conversation_id: str,
    drafts: dict,
    prospect_name: str = "",
    company: str = "",
    selected_variant: Optional[str] = None,
) -> bool:
    """Store generated draft variants as a FollowUpDraftRecord. Best-effort."""
    try:
        from . import get_db
        from .models import FollowUpDraftRecord

        with get_db() as db:
            record = FollowUpDraftRecord(
                conversation_id=conversation_id,
                prospect_name=prospect_name or None,
                company=company or None,
                variant_agreeable=drafts.get("variant_agreeable", ""),
                variant_direct=drafts.get("variant_direct", ""),
                variant_soft=drafts.get("variant_soft", ""),
                selected_variant=selected_variant,
            )
            db.add(record)
        try:
            from .audit import audit
            audit.log(
                "draft:generated",
                entity_type="draft",
                entity_id=conversation_id,
                details={"selected_variant": selected_variant},
            )
        except Exception:
            pass
        return True
    except Exception as e:
        logger.debug(f"persist_followup_draft skipped for {conversation_id}: {e}")
        return False
