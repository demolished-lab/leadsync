"""Email tracking utilities - pixel generation and click tracking."""

import hashlib
import json
import re
import urllib.parse
from datetime import datetime
from typing import Optional, Dict, Any


class EmailTracking:
    """Email tracking metadata for a prospect."""
    
    def __init__(self, prospect_id: str, email_address: str):
        self.prospect_id = prospect_id
        self.email_address = email_address
        self.opens: int = 0
        self.clicks: int = 0
        self.last_opened: Optional[datetime] = None
        self.last_clicked: Optional[datetime] = None
        self.suppressed: bool = False
        self._open_tracking_id = hashlib.md5(email_address.encode()).hexdigest()[:12]
        self._click_tracking_id = hashlib.md5(email_address.encode()).hexdigest()[12:24]

    def pixel_img_tag(self, tracking_url: str) -> str:
        """Generate an HTML img tag for open tracking."""
        return f'<img src="{tracking_url}" width="1" height="1" style="display:none;" alt="">'

    def log_open(self) -> None:
        """Log an email open event."""
        self.opens += 1
        from datetime import datetime
        self.last_opened = datetime.utcnow()

    def log_click(self, link_url: str) -> None:
        """Log a click event on a tracked link."""
        self.clicks += 1
        from datetime import datetime
        self.last_clicked = datetime.utcnow()

    def engagement_score(self) -> float:
        """Calculate engagement score (0-1 scale)."""
        # Weight: opens=0.4, clicks=0.6
        if self.opens == 0 and self.clicks == 0:
            return 0.0
        base = min((self.opens * 0.4 + self.clicks * 0.6) / 10.0, 1.0)
        return round(base, 2)

    def is_suppressed(self) -> bool:
        """Check if this email is on the suppression list."""
        return self.suppressed

    def suppress(self) -> None:
        """Mark this email as suppressed (unsubscribe)."""
        self.suppressed = True


def generate_tracking_pixel_url(base_url: str, prospect_id: str, email_hash: str) -> str:
    """Generate a tracking pixel URL."""
    parsed = urllib.parse.urlparse(base_url)
    query_params = dict(urllib.parse.parse_qsl(parsed.query))
    query_params['p'] = prospect_id
    query_params['h'] = email_hash
    new_query = urllib.parse.urlencode(query_params)
    new_url = urllib.parse.urlunparse(parsed._replace(query=new_query))
    return new_url


def generate_tracking_link_url(base_url: str, prospect_id: str, email_hash: str, original_url: str) -> str:
    """Generate a tracked link URL."""
    parsed = urllib.parse.urlparse(base_url)
    query_params = dict(urllib.parse.parse_qsl(parsed.query))
    query_params['p'] = prospect_id
    query_params['h'] = email_hash
    query_params['l'] = original_url
    new_query = urllib.parse.urlencode(query_params)
    new_url = urllib.parse.urlunparse(parsed._replace(query=new_query))
    return new_url


def parse_tracking_url(url: str) -> Dict[str, str]:
    """Parse a tracking URL to extract metadata."""
    parsed = urllib.parse.urlparse(url)
    return dict(urllib.parse.parse_qsl(parsed.query))


def _persist_tracking_event(prospect_id: str, event: str, details: Dict[str, Any]) -> None:
    """
    Best-effort persistence of a tracking event.

    Two sinks, both optional:
      1. The audit trail (core.database.audit) — always attempted.
      2. FollowUpDraftRecord counters (opens/clicks) — only when a draft
         record with a matching conversation_id exists.

    Never raises: tracking must not break the request that triggered it.
    """
    # 1) Audit trail
    try:
        from ..database.audit import audit

        audit.log(
            action=f"tracking:{event}",
            entity_type="prospect",
            entity_id=prospect_id,
            details=details,
            actor="tracking",
        )
    except Exception:
        pass  # audit may be unavailable (e.g. no sqlalchemy) — never raise

    # 2) FollowUpDraftRecord counters (best effort, only if a record exists)
    try:
        from ..database import get_db
        from ..database.models import FollowUpDraftRecord

        with get_db() as db:
            record = (
                db.query(FollowUpDraftRecord)
                .filter(FollowUpDraftRecord.conversation_id == prospect_id)
                .first()
            )
            if record is not None:
                if event == "open":
                    record.opens = (record.opens or 0) + 1
                elif event == "click":
                    record.clicks = (record.clicks or 0) + 1
    except Exception:
        pass  # record may not exist or DB may be unavailable — never raise


def log_open_event(prospect_id: str, email_address: str) -> Dict[str, Any]:
    """Log an email open event and return tracking data."""
    import hashlib
    email_hash = hashlib.md5(email_address.encode()).hexdigest()[:12]
    result = {
        'prospect_id': prospect_id,
        'email_hash': email_hash,
        'event': 'open',
        'timestamp': datetime.utcnow().isoformat(),
    }
    # Persist (audit trail + draft-record counters) without changing the
    # in-memory return-value contract.
    _persist_tracking_event(prospect_id, 'open', {
        'email_hash': email_hash,
        'timestamp': result['timestamp'],
        'last_opened': result['timestamp'],
    })
    return result


def log_click_event(prospect_id: str, email_address: str, link_url: str) -> Dict[str, Any]:
    """Log a click event and return tracking data."""
    import hashlib
    email_hash = hashlib.md5(email_address.encode()).hexdigest()[12:24]
    result = {
        'prospect_id': prospect_id,
        'email_hash': email_hash,
        'event': 'click',
        'link_url': link_url,
        'timestamp': datetime.utcnow().isoformat(),
    }
    # Persist (audit trail + draft-record counters) without changing the
    # in-memory return-value contract.
    _persist_tracking_event(prospect_id, 'click', {
        'email_hash': email_hash,
        'link_url': link_url,
        'timestamp': result['timestamp'],
        'last_clicked': result['timestamp'],
    })
    return result


def update_prospect_response_status(
    prospect_id: str, 
    email_address: str, 
    event: str, 
    additional_data: Optional[Dict] = None
) -> Dict[str, Any]:
    """Update prospect response status based on email interaction."""
    if event == 'open':
        return log_open_event(prospect_id, email_address)
    elif event == 'click':
        return log_click_event(prospect_id, email_address, additional_data.get('link_url', '')) if additional_data else log_click_event(prospect_id, email_address, '')
    return {}


def calculate_engagement_score(opens: int, clicks: int) -> float:
    """Calculate engagement score from open/click counts."""
    if opens == 0 and clicks == 0:
        return 0.0
    base = min((opens * 0.4 + clicks * 0.6) / 10.0, 1.0)
    return round(base, 2)


def format_pixel_img_tag(tracking_url: str) -> str:
    """Generate HTML img tag for pixel tracking."""
    return f'<img src="{tracking_url}" width="1" height="1" style="display:none;" alt="">'