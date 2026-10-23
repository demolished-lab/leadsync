"""
Gmail API via Google OAuth — like Macro (Sign in with Google).
Falls back to IMAP when no token; actual functionality works either way.
"""
import os, json, base64
from pathlib import Path
from typing import List, Optional
from datetime import datetime
import logging

logger = logging.getLogger("GmailAPI")

SCOPES = [
    "https://www.googleapis.com/auth/gmail.readonly",
    "https://www.googleapis.com/auth/gmail.send",
    "https://www.googleapis.com/auth/userinfo.email",
]

def _token_path() -> Path:
    try:
        from ..config import get_settings
        return Path(get_settings().gmail_token_path)
    except:
        return Path(os.environ.get("GMAIL_TOKEN_PATH", "data/gmail_token.json"))

# ── OAuth state store (CSRF protection) ───────────────────────
import secrets as _secrets
import threading as _auth_lock

_STATE_TTL_SECONDS = 600
_oauth_states: dict = {}  # state -> expiry (monotonic)
_states_lock = _auth_lock.Lock()


def _new_oauth_state() -> str:
    """Create a one-time state param that must round-trip via Google."""
    state = _secrets.token_urlsafe(32)
    with _states_lock:
        now = _time_monotonic()
        # purge expired
        for k in [k for k, exp in _oauth_states.items() if now > exp]:
            _oauth_states.pop(k, None)
        _oauth_states[state] = now + _STATE_TTL_SECONDS
    return state


def _consume_oauth_state(state: Optional[str]) -> bool:
    """Validate and consume a state token (single use)."""
    if not state:
        return False
    with _states_lock:
        expiry = _oauth_states.pop(state, None)
    return expiry is not None and _time_monotonic() <= expiry


def _time_monotonic():
    import time
    return time.time()


def get_auth_url(state: Optional[str] = None) -> str:
    """Build the Google consent URL. When `state` is given it must be an
    unused token created via create_oauth_state(); it round-trips through
    Google and is verified in the callback."""
    from ..config import get_settings
    s = get_settings()
    if not s.google_client_id or not s.google_client_secret:
        raise RuntimeError("GOOGLE_CLIENT_ID/SECRET not set — add to .env (Google Cloud → APIs & Services → Credentials → OAuth 2.0 Client ID)")
    from google_auth_oauthlib.flow import Flow
    flow = Flow.from_client_config(
        {
            "web": {
                "client_id": s.google_client_id,
                "client_secret": s.google_client_secret,
                "auth_uri": "https://accounts.google.com/o/oauth2/auth",
                "token_uri": "https://oauth2.googleapis.com/token",
                "redirect_uris": [s.google_redirect_uri],
            }
        },
        scopes=SCOPES,
        redirect_uri=s.google_redirect_uri,
    )
    kwargs = {}
    if state:
        kwargs["state"] = state
    url, actual_state = flow.authorization_url(
        access_type="offline", prompt="consent", include_granted_scopes="true", **kwargs
    )
    return url


def create_oauth_state() -> str:
    """Create and register a single-use OAuth state token."""
    return _new_oauth_state()


def handle_callback(code: str, redirect_uri: Optional[str] = None, state: Optional[str] = None) -> dict:
    # CSRF: reject callbacks without a valid, previously-created state token
    if not _consume_oauth_state(state):
        raise RuntimeError("Invalid or missing OAuth state parameter (CSRF protection)")
    from ..config import get_settings
    s = get_settings()
    from google_auth_oauthlib.flow import Flow
    flow = Flow.from_client_config(
        {
            "web": {
                "client_id": s.google_client_id,
                "client_secret": s.google_client_secret,
                "auth_uri": "https://accounts.google.com/o/oauth2/auth",
                "token_uri": "https://oauth2.googleapis.com/token",
                "redirect_uris": [redirect_uri or s.google_redirect_uri],
            }
        },
        scopes=SCOPES,
        redirect_uri=redirect_uri or s.google_redirect_uri,
    )
    flow.fetch_token(code=code)
    creds = flow.credentials
    # get email
    email = ""
    try:
        from googleapiclient.discovery import build
        svc = build("oauth2", "v2", credentials=creds)
        email = svc.userinfo().get().execute().get("email", "")
    except: pass
    data = {
        "token": creds.token,
        "refresh_token": getattr(creds, "refresh_token", None),
        "token_uri": creds.token_uri,
        "client_id": creds.client_id,
        "client_secret": creds.client_secret,
        "scopes": list(creds.scopes or SCOPES),
        "expiry": creds.expiry.isoformat() if getattr(creds, "expiry", None) else None,
        "email": email,
    }
    p = _token_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(data, indent=2))
    # Restrict the token file to the owner (no group/world read)
    try:
        os.chmod(p, 0o600)
    except Exception:
        pass
    logger.info(f"Gmail OAuth saved for {email} -> {p}")
    return data

def _load_creds():
    p = _token_path()
    if not p.exists():
        return None, None
    data = json.loads(p.read_text())
    from google.oauth2.credentials import Credentials
    creds = Credentials(
        token=data.get("token"),
        refresh_token=data.get("refresh_token"),
        token_uri=data.get("token_uri"),
        client_id=data.get("client_id"),
        client_secret=data.get("client_secret"),
        scopes=data.get("scopes"),
    )
    # refresh if needed
    try:
        if creds.expired and creds.refresh_token:
            from google.auth.transport.requests import Request
            creds.refresh(Request())
            data["token"] = creds.token
            data["expiry"] = creds.expiry.isoformat() if creds.expiry else None
            p.write_text(json.dumps(data, indent=2))
    except Exception as e:
        logger.warning(f"Token refresh failed: {e}")
    return creds, data.get("email", "")

def is_connected() -> bool:
    return _token_path().exists()

def get_connected_email() -> str:
    _, email = _load_creds()
    return email or ""

def disconnect():
    p = _token_path()
    if p.exists():
        p.unlink()
        logger.info("Gmail disconnected")

def fetch_via_api(limit: int = 10):
    """Fetch emails via Gmail API and convert to Conversation objects."""
    creds, _ = _load_creds()
    if not creds:
        raise RuntimeError("Gmail not connected — Sign in with Google first")
    from googleapiclient.discovery import build
    from email.utils import parsedate_to_datetime
    from ..models.conversation import Conversation, ExtractedEntity
    import base64, email as email_lib

    svc = build("gmail", "v1", credentials=creds)
    msgs = svc.users().messages().list(userId="me", maxResults=limit, q="").execute().get("messages", [])
    out = []
    for m in msgs:
        raw = svc.users().messages().get(userId="me", id=m["id"], format="raw").execute()
        b64 = raw.get("raw", "")
        # Gmail raw is base64url
        try:
            msg_bytes = base64.urlsafe_b64decode(b64 + "==")
            msg = email_lib.message_from_bytes(msg_bytes)
        except:
            continue
        # parse
        def _hdr(k): 
            try: return msg.get(k, "")
            except: return ""
        subject = _hdr("Subject")
        from_ = _hdr("From")
        date_s = _hdr("Date")
        # body
        body = ""
        if msg.is_multipart():
            for part in msg.walk():
                if part.get_content_type()=="text/plain":
                    try: body = part.get_payload(decode=True).decode(errors="replace"); break
                    except: continue
        else:
            try: body = msg.get_payload(decode=True).decode(errors="replace") if msg.get_payload(decode=True) else msg.get_payload()
            except: body = str(msg.get_payload())
        if not body or not body.strip():
            continue
        try:
            dt = parsedate_to_datetime(date_s).replace(tzinfo=None) if date_s else datetime.utcnow()
        except: dt = datetime.utcnow()
        # participants
        import re
        participants=[]
        pat=r'[a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,}'
        for hdr in [from_, msg.get("To","")]:
            for em in re.findall(pat, str(hdr)):
                participants.append({"name": em.split("@")[0].title(), "email": em})
        if not participants: participants=[{"name":"Unknown","email":"unknown"}]
        # heuristic
        try:
            from .email import _heuristic_urgency_and_deal
            urgency,_deal=_heuristic_urgency_and_deal(subject+" "+body)
        except: urgency="low"; _deal=None
        conv = Conversation(source="email", participants=participants, date=dt, raw_text=body.strip(), commitments=[], entities=ExtractedEntity(), sentiment="neutral", deal_size=_deal, urgency=urgency)
        # keep original subject for scoring
        conv.entities.pain_point = subject
        out.append(conv)
    return out

def send_via_api(to: str, subject: str, body: str) -> dict:
    creds, _ = _load_creds()
    if not creds:
        raise RuntimeError("Gmail not connected")
    from googleapiclient.discovery import build
    import base64
    from email.mime.text import MIMEText
    svc = build("gmail", "v1", credentials=creds)
    msg = MIMEText(body, "plain", "utf-8")
    msg["To"] = to
    msg["Subject"] = subject
    raw = base64.urlsafe_b64encode(msg.as_bytes()).decode()
    sent = svc.users().messages().send(userId="me", body={"raw": raw}).execute()
    return {"status": "sent", "id": sent.get("id"), "to": to}
