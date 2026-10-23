"""FastAPI application for the AI Sales Follow-Up Agent."""

import hashlib
import logging
import os
import sys
import time as _time
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from fastapi import FastAPI, Form, HTTPException, Query, Request, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, Response, StreamingResponse

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.config import get_settings
from core.email_tracking import (
    EmailTracking,
    calculate_engagement_score,
    generate_tracking_link_url,
    generate_tracking_pixel_url,
)
from core.generation.prompt import generate_drafts, select_draft
from core.ingest.email import (
    add_suppression,
    fetch_emails,
    is_suppressed,
    parse_email_to_conversation,
    send_email,
)
from core.ingest.meeting import process_meeting_notes
from core.ingest.stt import process_call_audio, transcribe_audio
from core.intelligence.action_engine import determine_next_best_action
from core.intelligence.llm_manager import llm_manager
from core.intelligence.scorer import (
    calculate_recency_decay,
    score_prospect,
    validate_score_inputs,
)
from core.models.conversation import Conversation
from core.models.prospect import ScoredProspect
from core.queue import PriorityQueue, get_queue
from core.database.persistence import (
    persist_conversation,
    persist_scored_prospect,
    persist_followup_draft,
)
from core.monitoring import metrics, record_request, record_queue_depth, record_sla_breach
from core.middleware import (
    RateLimiter,
    RateLimitMiddleware,
    WebSocketRateLimiter,
    get_api_key_verifier,
)

logger = logging.getLogger("API")
settings = get_settings()

# ── Rate Limiters ─────────────────────────────────────────────
http_rate_limiter = RateLimitMiddleware(
    default_max_requests=120,
    default_window_seconds=60,
    route_settings={
        "/pipeline/process": {"max_requests": 10, "window_seconds": 60},
        "/ingest/emails": {"max_requests": 5, "window_seconds": 60},
        "/ingest/call": {"max_requests": 10, "window_seconds": 60},
        "/drafts/generate": {"max_requests": 20, "window_seconds": 60},
        "/send/follow-up": {"max_requests": 15, "window_seconds": 60},
        "/sla/check": {"max_requests": 10, "window_seconds": 60},
        # Auth brute-force protection
        "/auth/2fa/verify": {"max_requests": 10, "window_seconds": 300},
        "/auth/2fa/backup-code": {"max_requests": 10, "window_seconds": 300},
        "/auth/login": {"max_requests": 10, "window_seconds": 300},
        "/auth/2fa/recovery/redeem": {"max_requests": 5, "window_seconds": 300},
    },
)
ws_rate_limiter = WebSocketRateLimiter(max_messages=30, window_seconds=60)
api_key_verifier = get_api_key_verifier()

app = FastAPI(
    title="AI Sales Follow-Up Agent",
    description="LeadSync - autonomous sales follow-up platform with intelligent LLM fallback",
    version="1.0.0",
)

# ── CORS — explicit allowlist (never wildcard + credentials) ──
_origins = [o.strip() for o in (settings.allowed_origins or "").split(",") if o.strip()]
if not _origins:
    # Default: the shipped Streamlit UI + common dev origins only.
    _origins = [
        "http://localhost:8501",
        "http://127.0.0.1:8501",
    ]
_wildcard = "*" in _origins
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"] if _wildcard else _origins,
    allow_credentials=False if _wildcard else True,
    allow_methods=["*"],
    allow_headers=["*"],
)


# ── Lifespan: initialize database + SLA checker ──────────────
from contextlib import asynccontextmanager
import threading as _thr

class GmailPoller:
    """Background Gmail IMAP poller — auto-fetches and queues new emails every N seconds."""
    def __init__(self, interval: int = 60):
        self.interval = interval
        self._stop = _thr.Event()
        self._thread: _thr.Thread | None = None
        self.last_run: str | None = None
        self.last_fetched = 0
        self.runs = 0

    def _is_configured(self) -> bool:
        try:
            from core.ingest.gmail_api import is_connected as _oa
            if _oa(): return True
        except: pass
        try:
            from core.config import get_settings
            s = get_settings()
            if getattr(s, "air_gapped", False):
                return False
            u = (getattr(s, "imap_username", "") or "").strip()
            p = (getattr(s, "imap_password", "") or "").strip()
            return bool(u and p and "@" in u and u != "test@leadsync.local")
        except: return False

    def poll_once(self) -> dict:
        # OAuth first
        try:
            from core.ingest.gmail_api import is_connected, fetch_via_api
            if is_connected():
                from core.intelligence.scorer import score_prospect
                from core.queue import get_queue
                convs = fetch_via_api(limit=10)
                q=get_queue()
                for c in convs:
                    try: q.add(score_prospect(c))
                    except: pass
                self.last_run = __import__("datetime").datetime.utcnow().isoformat()
                self.last_fetched=len(convs); self.runs+=1
                logger.info(f"Gmail poll via OAuth: {len(convs)}")
                return {"fetched": len(convs), "queued": len(convs), "via": "gmail_api"}
        except Exception as e:
            logger.warning(f"Gmail OAuth poll failed: {e}")
        if not self._is_configured():
            return {"skipped": True, "reason": "Gmail not configured or AIR_GAPPED"}
        try:
            from core.config import get_settings
            from core.ingest.email import fetch_emails
            from core.intelligence.scorer import score_prospect
            from core.queue import get_queue
            s = get_settings()
            convs = fetch_emails(s.imap_host, s.imap_port, s.imap_username, s.imap_password, limit=10)
            q = get_queue()
            added = 0
            for c in convs:
                try:
                    scored = score_prospect(c)
                    q.add(scored)
                    added += 1
                except Exception: continue
            self.last_run = __import__("datetime").datetime.utcnow().isoformat()
            self.last_fetched = len(convs)
            self.runs += 1
            logger.info(f"Gmail poll: fetched {len(convs)}, queued {added}")
            return {"fetched": len(convs), "queued": added}
        except Exception as e:
            logger.warning(f"Gmail poll failed: {e}")
            return {"error": str(e)}

    def start(self):
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        def loop():
            while not self._stop.wait(self.interval):
                try: self.poll_once()
                except Exception as e: logger.warning(f"Gmail poll loop error: {e}")
        self._thread = _thr.Thread(target=loop, name="gmail-poller", daemon=True)
        self._thread.start()
        logger.info(f"Gmail poller started (every {self.interval}s)")

    def stop(self):
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=3)

_gmail_poller: GmailPoller | None = None
def get_gmail_poller() -> GmailPoller:
    global _gmail_poller
    if _gmail_poller is None:
        import os
        interval = int(os.environ.get("GMAIL_POLL_INTERVAL", "60"))
        _gmail_poller = GmailPoller(interval=interval)
    return _gmail_poller

@asynccontextmanager
async def lifespan(app: FastAPI):
    # startup
    try:
        from core.database import init_db
        init_db()
        logger.info("Database initialized")
    except Exception as e:
        logger.warning(f"Database init failed (non-fatal): {e}")
    try:
        from core.alerts import get_sla_checker
        checker = get_sla_checker()
        checker.start()
        logger.info("SLA breach checker started")
    except Exception as e:
        logger.warning(f"SLA checker start failed (non-fatal): {e}")
    # Gmail auto-poll (only if configured, not air-gapped)
    try:
        poller = get_gmail_poller()
        # do one immediate poll in background after 5s
        def _delayed():
            import time as _t
            _t.sleep(5)
            if not poller._stop.is_set():
                poller.poll_once()
        _thr.Thread(target=_delayed, daemon=True).start()
        poller.start()
    except Exception as e:
        logger.warning(f"Gmail poller start failed (non-fatal): {e}")
    yield
    # shutdown
    try:
        from core.alerts import get_sla_checker
        get_sla_checker().stop()
    except Exception:
        pass
    try:
        get_gmail_poller().stop()
    except: pass

# attach lifespan via the supported parameter (no on_event re-entry hazard)
app.router.lifespan_context = lifespan


# ── Request metrics middleware ────────────────────────────────
from starlette.middleware.base import BaseHTTPMiddleware


class MetricsMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request, call_handler):
        start = _time.time()
        response = await call_handler(request)
        duration = _time.time() - start
        record_request(
            method=request.method,
            path=request.url.path,
            status=response.status_code,
            duration=duration,
        )
        return response


app.add_middleware(MetricsMiddleware)


# ── API Key Authentication Middleware ─────────────────────────
# Paths that are always public (read-only system surface, email-client
# tracking callbacks, one-time recovery redemption):
_PUBLIC_PATHS = {
    "/",
    "/health",
    "/metrics",
    "/docs",
    "/redoc",
    "/openapi.json",
    "/rate-limit/status",
    "/ws/stats",
    "/events/queue",  # SSE — read-only realtime feed (viewer access)
    "/tracking/click",  # called by end-user email clients
}

_API_AUTH_CONFIGURED: Optional[bool] = None


def _api_keys_configured() -> bool:
    """True if at least one API key is defined (env or auth config)."""
    global _API_AUTH_CONFIGURED
    if _API_AUTH_CONFIGURED is None:
        if (settings.api_keys or "").strip():
            _API_AUTH_CONFIGURED = True
        else:
            try:
                keys = api_key_verifier._load_keys()
                _API_AUTH_CONFIGURED = bool(keys)
            except Exception:
                _API_AUTH_CONFIGURED = False
    return _API_AUTH_CONFIGURED


class AuthHTTPMiddleware(BaseHTTPMiddleware):
    """
    Enforces API-key authentication when AUTH_REQUIRED=true.

    - Invalid key → 401; no keys configured at all → 503 (fail-closed).
    - OPTIONS preflight passes through for CORS.
    - WebSocket endpoints also enforce this in their own handlers.
    """

    async def dispatch(self, request, call_handler):
        if not settings.auth_required:
            return await call_handler(request)

        if request.method == "OPTIONS":
            return await call_handler(request)

        path = request.url.path
        if path in _PUBLIC_PATHS or path.startswith(("/tracking/pixel/", "/auth/google/callback", "/auth/2fa/recovery/redeem")):
            return await call_handler(request)

        api_key = (
            request.headers.get("x-api-key")
            or request.query_params.get("api_key")
        )
        if not api_key:
            auth_header = request.headers.get("authorization", "")
            if auth_header.lower().startswith("bearer "):
                token = auth_header[7:].strip()
                try:
                    from core.auth import get_authenticator
                    user = get_authenticator().verify_session(token)
                    if user is not None:
                        return await call_handler(request)
                except Exception:
                    pass

        if not api_key:
            if not _api_keys_configured():
                return JSONResponse(
                    status_code=503,
                    content={
                        "error": "api_auth_not_configured",
                        "message": (
                            "Authentication is required but no API keys are configured. "
                            "Set API_KEYS=<raw-key-1>,<raw-key-2> (env/.env), or run "
                            "`python -m core.auth create-key <name>`, or set AUTH_REQUIRED=false "
                            "for local-only development."
                        ),
                    },
                )
            return JSONResponse(
                status_code=401,
                content={"error": "unauthorized", "message": "Valid X-API-Key required"},
                headers={"WWW-Authenticate": "ApiKey"},
            )

        metadata = api_key_verifier.verify(api_key)
        if metadata is None:
            return JSONResponse(
                status_code=401,
                content={"error": "unauthorized", "message": "Invalid API key"},
                headers={"WWW-Authenticate": "ApiKey"},
            )
        return await call_handler(request)


app.add_middleware(AuthHTTPMiddleware)


# ── Rate Limit Middleware ──────────────────────────────────────
from core.middleware import resolve_client_ip
class RateLimitHTTPMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request, call_handler):
        client_ip = resolve_client_ip(request)
        path = request.url.path

        allowed, info = http_rate_limiter.check(path, client_ip)
        if not allowed:
            return JSONResponse(
                status_code=429,
                content={
                    "error": "rate_limit_exceeded",
                    "message": f"Rate limit exceeded. Try again in {info['retry_after']}s.",
                    **info,
                },
                headers={
                    "X-RateLimit-Limit": str(info["limit"]),
                    "X-RateLimit-Remaining": str(info["remaining"]),
                    "X-RateLimit-Reset": info["reset_at"],
                    "Retry-After": str(info["retry_after"]),
                },
            )

        response = await call_handler(request)
        # Add rate limit headers to successful responses
        if hasattr(response, "headers"):
            response.headers["X-RateLimit-Limit"] = str(info.get("limit", 120))
            response.headers["X-RateLimit-Remaining"] = str(info.get("remaining", ""))
        return response


app.add_middleware(RateLimitHTTPMiddleware)


# ========== Health & System ==========


@app.get("/", tags=["root"])
async def root() -> Dict[str, Any]:
    return {
        "message": "AI Sales Follow-Up Agent API",
        "version": "1.0.0",
        "docs": "/docs",
        "status": "operational",
    }


@app.get("/health", tags=["system"])
async def health_check() -> Dict[str, Any]:
    queue = get_queue()
    llm_available = False
    try:
        llm_available = llm_manager.is_local_available()
    except Exception:
        pass
    llm_status = "available" if llm_available else "cloud-only"
    try:
        llm_health_data = llm_manager.get_health_report()
    except Exception:
        llm_health_data = {}

    return {
        "status": "healthy",
        "version": "1.0.0",
        "queue_depth": queue.size(),
        "llm_provider": settings.llm_provider,
        "llm_status": llm_status,
        "llm_health": llm_health_data,
        "imap_configured": bool(settings.imap_username),
        "smtp_configured": bool(settings.smtp_username),
        "timestamp": datetime.utcnow().isoformat(),
    }


@app.get("/metrics", tags=["system"])
async def prometheus_metrics() -> Response:
    """Prometheus metrics endpoint. Returns text in Prometheus exposition format."""
    # Update gauges before export
    queue = get_queue()
    stats = queue.get_queue_stats()
    record_queue_depth(stats["total_items"])
    record_sla_breach(stats["breached_count"])
    return Response(content=metrics.export(), media_type="text/plain; version=0.0.4; charset=utf-8")


# ========== Real-time WebSocket & SSE ==========

from core.realtime import ws_manager, event_bus, format_sse_event
import asyncio


@app.websocket("/ws/queue")
async def websocket_queue(
    websocket: WebSocket,
    api_key: Optional[str] = Query(None),
):
    """
    WebSocket endpoint for real-time queue updates.

    Authentication: Pass `?api_key=xxx` as query param.
    Rate limited: 30 messages/minute per connection.

    Clients receive JSON events:
    {
        "type": "queue:added" | "queue:popped" | "queue:removed" | "queue:breach",
        "data": { "conversation_id": ..., "priority_score": ..., ... },
        "timestamp": "..."
    }

    Also sends periodic heartbeat (every 30s) to keep connection alive.
    """
    # ── API Key Authentication ──────────────────────────────
    auth_metadata = None
    if api_key:
        auth_metadata = api_key_verifier.verify(api_key)
        if auth_metadata is None:
            await websocket.accept()
            await websocket.send_json({
                "type": "error",
                "data": {"message": "Invalid API key", "code": 4003},
            })
            await websocket.close(code=4003, reason="Invalid API key")
            return
    elif settings.auth_required:
        # API authentication is enforced — reject anonymous connections.
        await websocket.accept()
        await websocket.send_json({
            "type": "error",
            "data": {
                "message": "API key required: pass ?api_key=xxx or set AUTH_REQUIRED=false",
                "code": 4001,
            },
        })
        await websocket.close(code=4001, reason="API key required")
        return
    else:
        # No API key provided — accept but mark as unauthenticated
        # (allows anonymous read-only access when AUTH_REQUIRED=false)
        auth_metadata = {"name": "anonymous", "role": "viewer"}
        logger.debug("WebSocket connected without API key (anonymous)")

    # ── Connect ─────────────────────────────────────────────
    await ws_manager.connect(websocket)

    # Send auth confirmation
    await ws_manager._send_to(websocket, {
        "type": "auth",
        "data": {
            "authenticated": api_key is not None,
            "user": auth_metadata.get("name", "anonymous"),
            "role": auth_metadata.get("role", "viewer"),
            "rate_limit": ws_rate_limiter.max_messages,
            "rate_window": ws_rate_limiter.window_seconds,
        },
        "timestamp": datetime.utcnow().isoformat(),
    })

    try:
        while True:
            # Wait for client messages (ping/pong or commands)
            try:
                data = await asyncio.wait_for(websocket.receive_text(), timeout=30.0)

                # ── Rate Limit Check ──────────────────────
                allowed, rate_info = ws_rate_limiter.is_allowed(websocket)
                if not allowed:
                    await ws_manager._send_to(websocket, {
                        "type": "error",
                        "data": {
                            "message": f"Rate limit exceeded. {rate_info['retry_after']}s until reset.",
                            "code": 429,
                            **rate_info,
                        },
                        "timestamp": datetime.utcnow().isoformat(),
                    })
                    continue

                # ── Command Handling ──────────────────────
                if data == "ping":
                    await ws_manager._send_to(websocket, {
                        "type": "pong",
                        "data": {
                            "connections": ws_manager.connection_count,
                            "rate_remaining": rate_info.get("remaining"),
                        },
                        "timestamp": datetime.utcnow().isoformat(),
                    })
                elif data == "stats":
                    q = get_queue()
                    stats = q.get_queue_stats()
                    await ws_manager._send_to(websocket, {
                        "type": "stats",
                        "data": stats,
                        "timestamp": datetime.utcnow().isoformat(),
                    })
                elif data == "whoami":
                    await ws_manager._send_to(websocket, {
                        "type": "whoami",
                        "data": auth_metadata,
                        "timestamp": datetime.utcnow().isoformat(),
                    })
                else:
                    # Unknown command
                    await ws_manager._send_to(websocket, {
                        "type": "error",
                        "data": {"message": f"Unknown command: {data}. Use 'ping', 'stats', or 'whoami'."},
                        "timestamp": datetime.utcnow().isoformat(),
                    })

            except asyncio.TimeoutError:
                # Send heartbeat
                try:
                    await ws_manager._send_to(websocket, {
                        "type": "heartbeat",
                        "data": {"connections": ws_manager.connection_count},
                        "timestamp": datetime.utcnow().isoformat(),
                    })
                except Exception:
                    break
    except WebSocketDisconnect:
        pass
    except Exception as e:
        logger.warning(f"WebSocket error: {e}")
    finally:
        ws_rate_limiter.remove(websocket)
        await ws_manager.disconnect(websocket)


@app.get("/events/queue", tags=["realtime"])
async def sse_queue_events():
    """
    Server-Sent Events endpoint for queue updates.

    Simpler than WebSocket — works with any HTTP client.
    Returns a stream of events:
        event: queue:added
        data: {"type": "queue:added", "data": {...}, "timestamp": "..."}

    Keep-alive comment sent every 15s.
    """
    import json

    async def event_generator():
        queue = asyncio.Queue()

        def on_event(event):
            try:
                queue.put_nowait(event)
            except Exception:
                pass

        # Subscribe to all queue events
        event_bus.on("queue:added", on_event)
        event_bus.on("queue:popped", on_event)
        event_bus.on("queue:removed", on_event)
        event_bus.on("queue:breach", on_event)

        try:
            while True:
                try:
                    event = await asyncio.wait_for(queue.get(), timeout=15.0)
                    yield format_sse_event(event["type"], event.get("data", {}))
                except asyncio.TimeoutError:
                    # Send keep-alive comment
                    yield f": heartbeat {datetime.utcnow().isoformat()}\n\n"
        finally:
            event_bus.off("queue:added", on_event)
            event_bus.off("queue:popped", on_event)
            event_bus.off("queue:removed", on_event)
            event_bus.off("queue:breach", on_event)

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


@app.get("/ws/stats", tags=["realtime"])
async def websocket_stats() -> Dict[str, Any]:
    """WebSocket manager stats (for monitoring)."""
    return {
        "active_connections": ws_manager.connection_count,
        "total_broadcasts": ws_manager.broadcast_count,
        "recent_events": len(event_bus.get_recent(100)),
    }


@app.get("/config", tags=["system"])
async def get_config() -> Dict[str, Any]:
    """
    Redacted configuration view (non-secret settings only).

    Secrets (passwords, API keys, TOTP secrets, OAuth tokens) are never
    included — only booleans indicating whether a secret is set.
    """
    auth_configured = _api_keys_configured()
    config: Dict[str, Any] = {
        "llm_provider": settings.llm_provider,
        "ollama_host": settings.ollama_host if settings.llm_provider == "ollama" else None,
        "sla_hours": {
            "high": settings.sla_high_urgency_hours,
            "medium": settings.sla_medium_urgency_hours,
            "low": settings.sla_low_urgency_hours,
        },
        "queue_max_items_per_rep": settings.queue_max_items_per_rep,
        "priority_score_decay_days": settings.priority_score_decay_days,
        "email": {
            "imap_configured": bool(settings.imap_username),
            "smtp_configured": bool(settings.smtp_username),
            "tracking_enabled": settings.email_tracking_enabled,
            "gmail_oauth_configured": bool(settings.google_client_id),
        },
        "compliance": {
            "gdpr": settings.gdpr_compliance_enabled,
            "ccpa": settings.ccpa_compliance_enabled,
            "air_gapped": settings.air_gapped,
        },
        "realtime": {
            "use_redis": settings.use_redis,
        },
        "database": {
            "configured": bool(settings.database_url) or True,  # falls back to engine default
        },
        "security": {
            "auth_required": settings.auth_required,
            "api_key_configured": auth_configured,
            "enforce_2fa_admin": settings.enforce_2fa_admin,
            "enforce_2fa_rep": settings.enforce_2fa_rep,
            "inspector_enabled": bool(getattr(settings, "inspector_enabled", True)),
            "allowed_origins": settings.allowed_origins or "localhost defaults",
        },
        "secrets_redacted": True,
    }
    return {"config": config, "provided_at": datetime.utcnow().isoformat()}


@app.get("/rate-limit/status", tags=["system"])
async def rate_limit_status() -> Dict[str, Any]:
    """Get rate limiter configuration and current usage."""
    return {
        "http": {
            "default_limit": http_rate_limiter._default.max_requests,
            "window_seconds": http_rate_limiter._default.window_seconds,
            "route_overrides": {
                path: {
                    "max_requests": cfg["max_requests"],
                    "window_seconds": cfg["window_seconds"],
                }
                for path, cfg in http_rate_limiter._route_settings.items()
            },
        },
        "websocket": {
            "max_messages": ws_rate_limiter.max_messages,
            "window_seconds": ws_rate_limiter.window_seconds,
        },
    }


# ========== Ingestion ==========


def _assert_public_imap_host(host: str) -> None:
    """
    Block SSRF: refuse caller-supplied IMAP hosts that resolve to private,
    loopback, link-local or otherwise non-public IP space.
    Bypass explicitly with ALLOW_PRIVATE_IMAP=true (self-hosted mail servers).
    """
    import ipaddress
    import socket

    if (os.environ.get("ALLOW_PRIVATE_IMAP", "") or "").lower() == "true":
        return
    try:
        infos = socket.getaddrinfo(host, None)
    except OSError:
        raise HTTPException(status_code=400, detail=f"Cannot resolve IMAP host: {host}")
    for info in infos:
        ip = ipaddress.ip_address(info[4][0])
        if (
            ip.is_private
            or ip.is_loopback
            or ip.is_link_local
            or ip.is_multicast
            or ip.is_reserved
            or ip.is_unspecified
        ):
            raise HTTPException(
                status_code=400,
                detail=f"Refusing non-public IMAP host {host} (resolved {ip}). "
                       "Set ALLOW_PRIVATE_IMAP=true if you intentionally use a self-hosted server.",
            )


async def _ingest_and_queue(conversations: List[Any], limit_note: str = "") -> Dict[str, Any]:
    """Score + persist + queue a batch of conversations (shared by ingest endpoints)."""
    queue = get_queue()
    queued = 0
    for conv in conversations:
        try:
            scored = score_prospect(conversation=conv)
            persist_conversation(conv)
            persist_scored_prospect(scored)
            queue.add(scored)
            queued += 1
        except Exception as e:
            logger.warning(f"Failed to score conversation {conv.id}: {e}")
    return {"queued": queued, "queue_size": queue.size()}


@app.post("/ingest/emails", tags=["ingestion"])
async def ingest_emails(
    imap_host: Optional[str] = None,
    imap_port: Optional[int] = None,
    username: Optional[str] = None,
    password: Optional[str] = None,
    mailbox: str = "INBOX",
    since_days: Optional[int] = None,
    limit: int = 50,
) -> Dict[str, Any]:
    host = imap_host or settings.imap_host
    port = imap_port or settings.imap_port
    user = username or settings.imap_username
    pwd = password or settings.imap_password

    if not user or not pwd:
        raise HTTPException(status_code=400, detail="IMAP username and password required")
    if host:
        _assert_public_imap_host(host)

    try:
        conversations = fetch_emails(
            imap_host=host,
            imap_port=port,
            username=user,
            password=pwd,
            mailbox=mailbox,
            since_days=since_days,
            limit=limit,
        )
        result = await _ingest_and_queue(conversations)
        return {
            "message": f"Fetched and queued {len(conversations)} conversations",
            "conversation_count": len(conversations),
            **result,
            "status": "completed",
            "timestamp": datetime.utcnow().isoformat(),
        }
    except PermissionError as e:
        raise HTTPException(status_code=401, detail=str(e))
    except ConnectionError as e:
        raise HTTPException(status_code=502, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Email ingestion failed: {e}")


@app.post("/ingest/email", tags=["ingestion"])
async def ingest_single_email(email_data: Dict[str, Any]) -> Dict[str, Any]:
    """Parse a single email dict into a Conversation and queue it."""
    try:
        # Check dedup before processing
        from core.dedup import get_dedup_store
        dedup = get_dedup_store()
        msg_id = str(email_data.get("message_id") or email_data.get("Message-ID") or "")
        sender = str(email_data.get("from") or email_data.get("sender") or "")
        subject = str(email_data.get("subject") or "")
        body = str(email_data.get("body") or "")

        if dedup.is_duplicate(message_id=msg_id or None, sender=sender, subject=subject, body=body):
            return {
                "message": "Duplicate email — skipped",
                "status": "duplicate",
                "timestamp": datetime.utcnow().isoformat(),
            }

        conv = parse_email_to_conversation(email_data)
        scored = score_prospect(conversation=conv)
        persist_conversation(conv)
        persist_scored_prospect(scored)
        queue = get_queue()
        queue.add(scored)
        return {
            "message": "Email parsed and queued",
            "conversation_id": conv.id,
            "priority_score": scored.priority_score,
            "status": "queued",
            "timestamp": datetime.utcnow().isoformat(),
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Email processing failed: {e}")


_UPLOAD_ALLOWED_EXT = (".wav", ".mp3", ".m4a", ".ogg", ".oga", ".flac", ".webm", ".mp4")
_UPLOAD_MAX_BYTES = 50 * 1024 * 1024  # 50 MB


@app.post("/ingest/call", tags=["ingestion"])
async def ingest_call(
    audio_path: Optional[str] = Form(None),
    model_size: str = Form("base"),
    language: Optional[str] = Form(None),
    prospect_name: Optional[str] = Form(None),
    file: Optional[Any] = None,
) -> Dict[str, Any]:
    """
    Process a call recording into a Conversation.

    Two ways to provide audio:
    1. Upload the file as multipart field ``file`` (recommended).
    2. Reference an existing on-disk ``audio_path`` — restricted to paths
       inside the working directory (no absolute paths, no traversal).
    """
    import os as _os
    import secrets as _secrets
    import tempfile as _tempfile
    import asyncio as _asyncio

    tmp_path: Optional[str] = None
    try:
        if file is not None and getattr(file, "filename", None):
            # ── Upload path ────────────────────────────────
            filename = _os.path.basename(file.filename or "call_audio")
            ext = _os.path.splitext(filename)[1].lower()
            if ext not in _UPLOAD_ALLOWED_EXT:
                raise HTTPException(
                    status_code=400,
                    detail=f"Unsupported audio format {ext!r}. Allowed: {', '.join(_UPLOAD_ALLOWED_EXT)}",
                )
            tmp_dir = _tempfile.mkdtemp(prefix="leadsync_upload_")
            tmp_path = _os.path.join(tmp_dir, f"{_secrets.token_hex(8)}{ext}")
            total = 0
            with open(tmp_path, "wb") as out:
                while True:
                    chunk = await file.read(1024 * 1024)
                    if not chunk:
                        break
                    total += len(chunk)
                    if total > _UPLOAD_MAX_BYTES:
                        raise HTTPException(status_code=413, detail="Uploaded audio exceeds 50MB limit")
                    out.write(chunk)
            audio_file = tmp_path
        elif audio_path:
            # ── On-disk path path (confined) ───────────────
            if ".." in audio_path or _os.path.isabs(audio_path):
                raise HTTPException(status_code=400, detail="Invalid audio_path")
            if not _os.path.exists(audio_path):
                raise HTTPException(status_code=404, detail=f"Audio file not found: {audio_path}")
            audio_file = audio_path
        else:
            raise HTTPException(status_code=400, detail="Provide a multipart 'file' upload or audio_path")

        conv = await _asyncio.to_thread(
            process_call_audio,
            audio_path=audio_file,
            model_size=model_size,
            language=language,
            prospect_name=prospect_name,
        )
        scored = score_prospect(conversation=conv)
        persist_conversation(conv)
        persist_scored_prospect(scored)
        get_queue().add(scored)
        return {
            "message": "Call processed and queued",
            "conversation_id": conv.id,
            "source": conv.source,
            "urgency": conv.urgency,
            "sentiment": conv.sentiment,
            "priority_score": scored.priority_score,
            "transcript_preview": conv.raw_text[:200] + "..." if len(conv.raw_text) > 200 else conv.raw_text,
            "timestamp": datetime.utcnow().isoformat(),
        }
    except HTTPException:
        raise
    except FileNotFoundError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except RuntimeError as e:
        raise HTTPException(status_code=422, detail=str(e))
    except HTTPException as e:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Call processing failed: {e}")
    finally:
        if tmp_path:
            try:
                import shutil as _shutil
                _shutil.rmtree(_os.path.dirname(tmp_path), ignore_errors=True)
            except Exception:
                pass


@app.post("/ingest/meeting", tags=["ingestion"])
async def ingest_meeting(
    notes: str = Form(...),
    source: str = Form("meeting"),
) -> Dict[str, Any]:
    try:
        conv = process_meeting_notes(notes=notes, source=source)
        scored = score_prospect(conversation=conv)
        persist_conversation(conv)
        persist_scored_prospect(scored)
        queue = get_queue()
        queue.add(scored)
        return {
            "message": "Meeting notes processed and queued",
            "conversation_id": conv.id,
            "commitments": conv.commitments,
            "urgency": conv.urgency,
            "priority_score": scored.priority_score,
            "timestamp": datetime.utcnow().isoformat(),
        }
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Meeting processing failed: {e}")


# ========== Scoring ==========


@app.post("/score", tags=["intelligence"])
async def score_conversation(conversation: Conversation) -> Dict[str, Any]:
    try:
        scored = score_prospect(conversation=conversation)
        action = determine_next_best_action(conversation=conversation)
        return {
            "conversation_id": scored.conversation_id,
            "priority_score": scored.priority_score,
            "recency_days": scored.recency_days,
            "engagement_probability": scored.engagement_probability,
            "deal_value_normalized": scored.deal_value_normalized,
            "urgency_score": scored.urgency_score,
            "sla_deadline": scored.sla_deadline.isoformat(),
            "sla_breached": scored.sla_breached,
            "next_action": action["action_type"],
            "timing": action["timing_recommendation"],
            "rationale": action["rationale"],
            "escalation": action["escalation_level"],
            "timestamp": datetime.utcnow().isoformat(),
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Scoring failed: {e}")


@app.get("/score/decay", tags=["intelligence"])
async def get_decay(days: float = 0.0) -> Dict[str, Any]:
    return {"days": days, "decay": calculate_recency_decay(days)}


# ========== Action Engine ==========


@app.post("/action/determine", tags=["intelligence"])
async def determine_action(
    conversation: Conversation,
    rep_workload: int = Query(0, ge=0),
    rep_closing_deals: int = Query(0, ge=0),
) -> Dict[str, Any]:
    try:
        context = {"rep_workload": rep_workload, "rep_closing_deals": rep_closing_deals}
        action = determine_next_best_action(conversation=conversation, pipeline_context=context)
        return {**action, "timestamp": datetime.utcnow().isoformat()}
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Action determination failed: {e}")


# ========== Draft Generation ==========


@app.post("/drafts/generate", tags=["generation"])
async def generate_drafts_endpoint(
    conversation: Conversation,
    prospect_name: str = Query(...),
    company: str = Query(""),
    role: str = Query(""),
    pain_points: List[str] = Query([]),
    followup_count: int = Query(0, ge=0),
    urgency_level: str = Query("medium"),
    use_llm: bool = Query(True),
) -> Dict[str, Any]:
    try:
        drafts = generate_drafts(
            conversation=conversation,
            prospect_name=prospect_name,
            company=company,
            role=role,
            pain_points=pain_points,
            followup_count=followup_count,
            urgency_level=urgency_level,
            use_llm=use_llm,
        )
        selected = select_draft(drafts=drafts, urgency_level=urgency_level)
        return {
            "message": "3 follow-up drafts generated",
            "variants": drafts,
            "selected_variant": selected,
            "urgency_level": urgency_level,
            "timestamp": datetime.utcnow().isoformat(),
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Draft generation failed: {e}")


# ========== Autonomous Pipeline ==========


@app.post("/pipeline/process", tags=["pipeline"])
async def process_conversation_pipeline(
    conversation: Conversation,
    prospect_name: str = Query("Prospect"),
    company: str = Query(""),
    role: str = Query(""),
    pain_points: List[str] = Query([]),
    deal_value: Optional[float] = Query(None),
) -> Dict[str, Any]:
    """Full autonomous pipeline: Score -> Action -> Generate -> Queue."""
    try:
        scored = score_prospect(conversation=conversation, deal_value=deal_value)
        action = determine_next_best_action(conversation=conversation)

        drafts = {}
        if action["action_type"] in ("close", "re-engage", "nurture"):
            drafts = generate_drafts(
                conversation=conversation,
                prospect_name=prospect_name,
                company=company,
                role=role,
                pain_points=pain_points,
                followup_count=0,
                urgency_level=conversation.urgency or "low",
            )

        queue = get_queue()
        persist_conversation(conversation)
        persist_scored_prospect(scored)
        queue.add(scored)

        selected = select_draft(drafts, urgency_level=conversation.urgency or "medium") if drafts else None
        if drafts:
            persist_followup_draft(
                conversation_id=conversation.id,
                drafts=drafts,
                prospect_name=prospect_name,
                company=company,
                selected_variant=selected,
            )

        return {
            "status": "success",
            "conversation_id": scored.conversation_id,
            "priority_score": scored.priority_score,
            "sla_deadline": scored.sla_deadline.isoformat(),
            "action": action,
            "drafts": drafts,
            "selected_variant": selected,
            "queue_size": queue.size(),
            "timestamp": datetime.utcnow().isoformat(),
        }
    except Exception as e:
        logger.error(f"Pipeline failed: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=f"Pipeline failed: {e}")


# ========== Queue ==========


@app.get("/queue/stats", tags=["queue"])
async def queue_stats() -> Dict[str, Any]:
    return get_queue().get_queue_stats()


@app.get("/queue/list", tags=["queue"])
async def queue_list() -> Dict[str, Any]:
    items = get_queue().list()
    return {
        "count": len(items),
        "items": [
            {
                "conversation_id": s.conversation_id,
                "priority_score": s.priority_score,
                "status": s.status,
                "sla_breached": s.sla_breached,
                "urgency": s.conversation.urgency if s.conversation else "unknown",
            }
            for s in items
        ],
    }


@app.post("/queue/check-sla", tags=["queue"])
async def check_sla_breaches() -> Dict[str, Any]:
    breaches = get_queue().check_sla_breaches()
    return {
        "breach_count": len(breaches),
        "breached_prospects": breaches,
        "checked_at": datetime.utcnow().isoformat(),
    }


@app.post("/queue/pop", tags=["queue"])
async def pop_next() -> Dict[str, Any]:
    item = get_queue().pop_next()
    if item is None:
        return {"message": "Queue is empty", "item": None}
    return {
        "message": "Popped next prospect",
        "conversation_id": item.conversation_id,
        "priority_score": item.priority_score,
        "status": item.status,
    }


@app.post("/queue/requeue/{conversation_id}", tags=["queue"])
async def requeue_prospect(conversation_id: str) -> Dict[str, Any]:
    result = get_queue().increment_requeue(conversation_id)
    if result is None:
        raise HTTPException(status_code=404, detail="Prospect not found in queue")
    return {"message": "Prospect re-queued", **result, "timestamp": datetime.utcnow().isoformat()}


@app.delete("/queue/{conversation_id}", tags=["queue"])
async def remove_from_queue(conversation_id: str) -> Dict[str, Any]:
    removed = get_queue().remove(conversation_id)
    if not removed:
        raise HTTPException(status_code=404, detail="Prospect not found")
    return {"message": "Removed from queue", "conversation_id": conversation_id}


# ========== Email Tracking ==========
# In-memory engagement tracker (flushes to DB via core.email_tracking)
_TRACKING: Dict[str, "EmailTracking"] = {}
_TRACKING_LOCK = _thr.Lock()


def _get_tracker(prospect_id: str, email_address: str = "") -> EmailTracking:
    with _TRACKING_LOCK:
        t = _TRACKING.get(prospect_id)
        if t is None:
            t = EmailTracking(prospect_id, email_address or "")
            _TRACKING[prospect_id] = t
        return t


def _lookup_prospect_email(prospect_id: str) -> str:
    """Best-effort email lookup: live queue first, then DB history."""
    try:
        item = get_queue().get_by_id(prospect_id)
        if item and item.conversation:
            return item.conversation.primary_email
    except Exception:
        pass
    try:
        from core.database import get_db
        from core.database.models import ConversationRecord
        with get_db() as db:
            rec = db.query(ConversationRecord).filter(ConversationRecord.id == prospect_id).first()
            if rec and isinstance(rec.participants, dict):
                emails = [p.get("email") for p in (rec.participants.get("participants") or rec.participants.get("list") or []) if isinstance(p, dict)]
                if not emails and isinstance(rec.participants, dict):
                    # participants stored as dict; try values
                    emails = [v.get("email") for v in rec.participants.values() if isinstance(v, dict)]
            elif rec and isinstance(rec.participants, list):
                emails = [p.get("email") for p in rec.participants if isinstance(p, dict)]
            else:
                emails = []
            return next((e for e in emails if e), "")
    except Exception:
        return ""


def _mark_queue_response(prospect_id: str, response: str) -> None:
    """Update a live queued prospect's response status after tracking signal."""
    try:
        item = get_queue().get_by_id(prospect_id)
        if item:
            item.response_status = response
            item.last_action_at = datetime.utcnow()
    except Exception:
        pass


@app.get("/tracking/pixel/{prospect_id}", tags=["tracking"])
async def tracking_pixel(
    prospect_id: str,
    h: Optional[str] = Query(None, description="Tracking hash (ignored, informational)"),
) -> Response:
    """
    1x1 transparent GIF tracking pixel.

    Records the open: in-memory engagement tracker + audit trail + DB
    counters (via core.email_tracking) and marks a live queued prospect
    as `opened`. Safe to call from email clients (no auth by design).
    """
    pixel_data = b"GIF89a\x01\x00\x01\x00\x80\x00\x00\xff\x0a\x00\x3b"
    email = _lookup_prospect_email(prospect_id)
    tracker = _get_tracker(prospect_id, email)
    tracker.log_open()
    try:
        from core.email_tracking import log_open_event
        log_open_event(prospect_id, email)
    except Exception:
        pass
    try:
        from core.database.audit import audit
        audit.log(
            "tracking:open",
            entity_type="prospect",
            entity_id=prospect_id,
            details={"opens": tracker.opens, "engagement_score": tracker.engagement_score()},
        )
    except Exception:
        pass
    _mark_queue_response(prospect_id, "opened")
    return Response(
        content=pixel_data,
        media_type="image/gif",
        headers={"Cache-Control": "no-store, no-cache, must-revalidate"},
    )


@app.post("/tracking/click", tags=["tracking"])
async def track_click(
    prospect_id: str = Form(...),
    link_url: str = Form(...),
) -> Dict[str, Any]:
    """
    Record a click on a tracked link: engagement tracker + audit + DB
    counters, then mark a live queued prospect as `clicked`.
    """
    email = _lookup_prospect_email(prospect_id)
    tracker = _get_tracker(prospect_id, email)
    tracker.log_click(link_url)
    try:
        from core.email_tracking import log_click_event
        log_click_event(prospect_id, email, link_url)
    except Exception:
        pass
    try:
        from core.database.audit import audit
        audit.log(
            "tracking:click",
            entity_type="prospect",
            entity_id=prospect_id,
            details={"link_url": link_url, "clicks": tracker.clicks, "engagement_score": tracker.engagement_score()},
        )
    except Exception:
        pass
    _mark_queue_response(prospect_id, "clicked")
    return {
        "status": "tracked",
        "prospect_id": prospect_id,
        "link_url": link_url,
        "clicks": tracker.clicks,
        "engagement_score": tracker.engagement_score(),
        "timestamp": datetime.utcnow().isoformat(),
    }


@app.get("/tracking/engagement/{prospect_id}", tags=["tracking"])
async def tracking_engagement(prospect_id: str) -> Dict[str, Any]:
    """Get open/click stats and engagement score for a prospect."""
    tracker = _TRACKING.get(prospect_id)
    if tracker is None:
        return {"prospect_id": prospect_id, "opens": 0, "clicks": 0, "engagement_score": 0.0, "status": "no_activity"}
    return {
        "prospect_id": prospect_id,
        "opens": tracker.opens,
        "clicks": tracker.clicks,
        "last_opened": tracker.last_opened.isoformat() if tracker.last_opened else None,
        "last_clicked": tracker.last_clicked.isoformat() if tracker.last_clicked else None,
        "engagement_score": tracker.engagement_score(),
        "status": "tracked",
    }


# ========== Follow-up Sending ==========


@app.post("/send/follow-up", tags=["send"])
async def send_follow_up(
    prospect_id: str = Form(...),
    to_address: str = Form(...),
    subject: str = Form(...),
    body: str = Form(...),
    variant: str = Form("direct"),
    include_tracking: bool = Form(True),
) -> Dict[str, Any]:
    # GDPR/CCPA: block suppressed
    if is_suppressed(to_address):
        raise HTTPException(status_code=403, detail=f"Recipient {to_address} is on suppression list (GDPR/CCPA)")
    tracking_url = None
    if include_tracking:
        tracking_url = generate_tracking_pixel_url(
            settings.tracking_base_url,
            prospect_id,
            hashlib.md5(to_address.encode()).hexdigest()[:12],
        )

    result = send_email(
        to_address=to_address,
        subject=subject,
        body=body,
        include_tracking_pixel=include_tracking,
        tracking_pixel_url=tracking_url,
    )

    # audit
    try:
        from core.database.audit import audit
        audit.log("email:sent" if result.get("status")=="sent" else "email:failed", entity_type="prospect", entity_id=prospect_id, details={"to": to_address, "variant": variant, "status": result.get("status")})
    except Exception:
        pass
    return {**result, "prospect_id": prospect_id, "variant": variant}


# ========== Suppressions ==========


@app.post("/suppressions/add", tags=["compliance"])
async def add_suppression_endpoint(email_address: str = Form(...)) -> Dict[str, Any]:
    if not email_address or "@" not in email_address:
        raise HTTPException(status_code=400, detail="Invalid email address")
    success = add_suppression(email_address)
    if not success:
        raise HTTPException(status_code=500, detail="Failed to add suppression")
    return {"message": f"Email {email_address} added to suppression list", "timestamp": datetime.utcnow().isoformat()}


@app.get("/suppressions/check/{email_address}", tags=["compliance"])
async def check_suppression(email_address: str) -> Dict[str, Any]:
    suppressed = is_suppressed(email_address)
    return {"email_address": email_address, "is_suppressed": suppressed, "checked_at": datetime.utcnow().isoformat()}


# ========== LLM Health ==========


@app.get("/llm/health", tags=["llm"])
async def llm_health() -> Dict[str, Any]:
    local_available = False
    try:
        local_available = llm_manager.is_local_available()
    except Exception:
        pass
    return {
        "local_available": local_available,
        "health_report": llm_manager.get_health_report(),
        "configured_provider": settings.llm_provider,
    }


@app.get("/llm/local-models", tags=["llm"])
async def local_models() -> Dict[str, Any]:
    models = llm_manager._get_ollama_models()
    return {"available": models, "count": len(models), "ollama_host": settings.ollama_host}


# ========== Database: History & Audit ==========


@app.get("/history/conversations", tags=["database"])
async def list_conversations(
    source: Optional[str] = Query(None),
    urgency: Optional[str] = Query(None),
    limit: int = Query(50, ge=1, le=500),
    offset: int = Query(0, ge=0),
) -> Dict[str, Any]:
    """List stored conversations with optional filters."""
    try:
        from core.database import get_db
        from core.database.models import ConversationRecord
        from sqlalchemy import desc

        with get_db() as db:
            query = db.query(ConversationRecord)
            if source:
                query = query.filter(ConversationRecord.source == source)
            if urgency:
                query = query.filter(ConversationRecord.urgency == urgency)
            total = query.count()
            items = (
                query.order_by(desc(ConversationRecord.ingested_at))
                .offset(offset)
                .limit(limit)
                .all()
            )
            return {
                "total": total,
                "offset": offset,
                "limit": limit,
                "items": [
                    {
                        "id": c.id,
                        "source": c.source,
                        "participants": c.participants,
                        "sentiment": c.sentiment,
                        "urgency": c.urgency,
                        "deal_size": c.deal_size,
                        "commitments": c.commitments,
                        "conversation_date": c.conversation_date.isoformat() if c.conversation_date else None,
                        "ingested_at": c.ingested_at.isoformat() if c.ingested_at else None,
                    }
                    for c in items
                ],
            }
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to list conversations: {e}")


@app.get("/history/conversations/{conversation_id}", tags=["database"])
async def get_conversation(conversation_id: str) -> Dict[str, Any]:
    """Get a single conversation by ID with full details."""
    try:
        from core.database import get_db
        from core.database.models import ConversationRecord

        with get_db() as db:
            conv = db.query(ConversationRecord).filter(ConversationRecord.id == conversation_id).first()
            if not conv:
                raise HTTPException(status_code=404, detail="Conversation not found")
            return {
                "id": conv.id,
                "source": conv.source,
                "participants": conv.participants,
                "raw_text": conv.raw_text,
                "commitments": conv.commitments,
                "entities": conv.entities,
                "sentiment": conv.sentiment,
                "deal_size": conv.deal_size,
                "urgency": conv.urgency,
                "tags": conv.tags,
                "conversation_date": conv.conversation_date.isoformat() if conv.conversation_date else None,
                "ingested_at": conv.ingested_at.isoformat() if conv.ingested_at else None,
            }
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to get conversation: {e}")


@app.post("/history/conversations", tags=["database"])
async def store_conversation(
    conversation: Conversation,
    tags: List[str] = Query([]),
) -> Dict[str, Any]:
    """Store a conversation to the database."""
    try:
        from core.database import get_db
        from core.database.models import ConversationRecord

        with get_db() as db:
            record = ConversationRecord(
                id=conversation.id,
                source=conversation.source,
                participants=conversation.participants,
                raw_text=conversation.raw_text,
                commitments=conversation.commitments,
                entities=conversation.entities.model_dump() if conversation.entities else {},
                sentiment=conversation.sentiment,
                deal_size=conversation.deal_size,
                urgency=conversation.urgency,
                conversation_date=conversation.date,
                tags=tags,
            )
            db.add(record)

            # Also audit
            from core.database.audit import audit
            audit.log(
                "conversation:stored",
                entity_type="conversation",
                entity_id=conversation.id,
                details={"source": conversation.source, "urgency": conversation.urgency},
            )

            return {
                "message": "Conversation stored",
                "id": record.id,
                "timestamp": datetime.utcnow().isoformat(),
            }
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to store conversation: {e}")


@app.get("/audit", tags=["database"])
async def audit_log(
    action: Optional[str] = Query(None),
    entity_type: Optional[str] = Query(None),
    entity_id: Optional[str] = Query(None),
    limit: int = Query(50, ge=1, le=500),
) -> Dict[str, Any]:
    """Query the audit trail."""
    try:
        from core.database.audit import audit as audit_logger

        entries = audit_logger.query(
            action=action,
            entity_type=entity_type,
            entity_id=entity_id,
            limit=limit,
        )
        return {
            "count": len(entries),
            "entries": entries,
            "stats": audit_logger.get_stats(),
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to query audit log: {e}")


@app.get("/audit/stats", tags=["database"])
async def audit_stats() -> Dict[str, Any]:
    """Get audit logging statistics."""
    from core.database.audit import audit as audit_logger
    return audit_logger.get_stats()


# ========== Gmail Auto-Poll ==========
@app.get("/gmail/status", tags=["gmail"])
async def gmail_status() -> Dict[str, Any]:
    poller = get_gmail_poller()
    try:
        from core.config import get_settings
        s = get_settings()
        cfg = bool((getattr(s, "imap_username","") or "").strip() and "@" in getattr(s, "imap_username",""))
        gmail = getattr(s, "imap_username","") or getattr(s, "smtp_username","")
        # also check OAuth
        try:
            from core.ingest.gmail_api import is_connected as _oa, get_connected_email as _ge
            if _oa():
                cfg=True; gmail=_ge() or gmail
                oauth=True
            else: oauth=False
        except: oauth=False
    except:
        cfg=False; gmail=""; oauth=False
    return {"configured": cfg, "gmail": gmail, "oauth_connected": oauth, "air_gapped": getattr(__import__("core.config", fromlist=["get_settings"]).get_settings(), "air_gapped", False), "interval": poller.interval, "last_run": poller.last_run, "last_fetched": poller.last_fetched, "runs": poller.runs, "alive": poller._thread.is_alive() if poller._thread else False}

@app.post("/gmail/poll", tags=["gmail"])
async def gmail_poll_now() -> Dict[str, Any]:
    # try OAuth first, then IMAP
    try:
        from core.ingest.gmail_api import is_connected, fetch_via_api
        if is_connected():
            from core.intelligence.scorer import score_prospect
            from core.queue import get_queue
            convs = fetch_via_api(limit=10)
            q=get_queue()
            for c in convs:
                try: q.add(score_prospect(c))
                except: pass
            return {"fetched": len(convs), "queued": len(convs), "via": "gmail_api"}
    except Exception as e:
        pass
    return get_gmail_poller().poll_once()

@app.get("/auth/google/url", tags=["auth"])
async def google_auth_url() -> Dict[str, Any]:
    """Start the Google OAuth flow — returns the consent URL containing a
    single-use CSRF state token that the callback must present back."""
    from core.ingest.gmail_api import get_auth_url, create_oauth_state
    try:
        state = create_oauth_state()
        url = get_auth_url(state=state)
        return {"url": url, "state": state}
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))

@app.get("/auth/google/callback", tags=["auth"])
async def google_callback(code: str = Query(...), state: Optional[str] = Query(None)) -> Dict[str, Any]:
    from core.ingest.gmail_api import handle_callback
    try:
        data = handle_callback(code, state=state)
        return {"connected": True, "email": data.get("email"), "message": f"Gmail connected as {data.get('email')}"}
    except RuntimeError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))

@app.get("/auth/google/status", tags=["auth"])
async def google_status() -> Dict[str, Any]:
    from core.ingest.gmail_api import is_connected, get_connected_email
    return {"connected": is_connected(), "email": get_connected_email()}

@app.post("/auth/google/disconnect", tags=["auth"])
async def google_disconnect() -> Dict[str, Any]:
    from core.ingest.gmail_api import disconnect
    disconnect()
    return {"disconnected": True}

# ========== SLA Breach Checker ==========


@app.get("/sla/status", tags=["sla"])
async def sla_checker_status() -> Dict[str, Any]:
    """Get SLA breach checker status."""
    from core.alerts import get_sla_checker
    return get_sla_checker().get_status()


@app.post("/sla/check", tags=["sla"])
async def sla_check_now() -> Dict[str, Any]:
    """Run an immediate SLA breach check."""
    from core.alerts import get_sla_checker
    return get_sla_checker().check_now()


@app.post("/sla/start", tags=["sla"])
async def sla_start() -> Dict[str, Any]:
    """Start the SLA breach checker background task."""
    from core.alerts import get_sla_checker
    checker = get_sla_checker()
    checker.start()
    return {"message": "SLA checker started", "status": checker.get_status()}


@app.post("/sla/stop", tags=["sla"])
async def sla_stop() -> Dict[str, Any]:
    """Stop the SLA breach checker background task."""
    from core.alerts import get_sla_checker
    checker = get_sla_checker()
    checker.stop()
    return {"message": "SLA checker stopped", "status": checker.get_status()}


@app.post("/sla/test-alert", tags=["sla"])
async def sla_test_alert() -> Dict[str, Any]:
    """Send a test alert through all configured channels."""
    from core.alerts import get_sla_checker, build_breach_alert
    from core.models.conversation import Conversation
    from core.models.prospect import ScoredProspect

    # Create a fake breached prospect for testing
    conv = Conversation(
        source="email",
        participants=[{"name": "Test Prospect", "email": "test@example.com"}],
        raw_text="This is a test alert.",
        urgency="high",
        deal_size=75000.0,
        commitments=["send proposal"],
    )
    prospect = ScoredProspect(
        conversation_id="test-alert-001",
        priority_score=0.92,
        conversation=conv,
        sla_deadline=datetime.utcnow(),
        sla_breached=True,
        times_requeued=4,
    )

    alert = build_breach_alert(prospect)
    checker = get_sla_checker()
    result = checker.alert_manager.send_alert(alert, "test-alert-001")

    return {
        "message": "Test alert sent",
        "alert": alert,
        "result": result,
    }


@app.get("/webhooks/channels", tags=["webhooks"])
async def webhook_channels() -> Dict[str, Any]:
    """List all configured webhook alert channels."""
    import os
    channels = []

    channel_config = [
        ("telegram", "TELEGRAM_BOT_TOKEN", "Telegram Bot API"),
        ("email", "ALERT_EMAIL", "SMTP Email"),
        ("slack", "SLACK_WEBHOOK_URL", "Slack Incoming Webhook"),
        ("discord", "DISCORD_WEBHOOK_URL", "Discord Webhook"),
        ("teams", "TEAMS_WEBHOOK_URL", "Microsoft Teams"),
        ("pagerduty", "PAGERDUTY_INTEGRATION_KEY", "PagerDuty Events API v2"),
        ("opsgenie", "OPSGENIE_API_KEY", "Opsgenie Alert API"),
    ]

    for slug, env_var, description in channel_config:
        configured = bool(os.environ.get(env_var, ""))
        channels.append({
            "slug": slug,
            "description": description,
            "env_var": env_var,
            "configured": configured,
        })

    return {
        "channels": channels,
        "total_configured": sum(1 for c in channels if c["configured"]),
        "timestamp": datetime.utcnow().isoformat(),
    }


@app.post("/webhooks/test", tags=["webhooks"])
async def webhook_test(
    channel: Optional[str] = Query(None, description="Channel to test (None = all)"),
    urgency: str = Query("high", description="Test urgency level"),
) -> Dict[str, Any]:
    """Send a test alert to one or all configured channels."""
    import os
    from core.alerts import (
        get_sla_checker, build_breach_alert,
        TelegramSender, EmailSender, SlackSender, DiscordSender,
        TeamsSender, PagerDutySender, OpsgenieSender,
    )
    from core.models.conversation import Conversation
    from core.models.prospect import ScoredProspect
    from core.config import get_settings

    # Build test prospect
    conv = Conversation(
        source="email",
        participants=[{"name": "Webhook Test", "email": "webhook-test@example.com"}],
        raw_text="This is a webhook test from the Sales Follow-Up Agent.",
        urgency=urgency,
        deal_size=50000.0,
        commitments=["test webhook integration"],
    )
    prospect = ScoredProspect(
        conversation_id=f"wh-test-{int(_time.time())}",
        priority_score=0.88,
        conversation=conv,
        sla_deadline=datetime.utcnow(),
        sla_breached=True,
        times_requeued=0,
    )
    alert = build_breach_alert(prospect)

    results = {}

    def _test_channel(slug: str, send_fn) -> Dict[str, Any]:
        try:
            start = _time.time()
            success = send_fn(alert)
            elapsed_ms = round((_time.time() - start) * 1000)
            return {
                "success": success,
                "latency_ms": elapsed_ms,
                "status": "sent" if success else "failed",
            }
        except Exception as e:
            return {"success": False, "error": str(e), "status": "error"}

    settings = get_settings()

    # Test specific channel or all
    channels_to_test = []
    if channel:
        channels_to_test.append(channel)
    else:
        channels_to_test = ["telegram", "email", "slack", "discord", "teams", "pagerduty", "opsgenie"]

    for ch in channels_to_test:
        if ch == "telegram":
            token = os.environ.get("TELEGRAM_BOT_TOKEN", "")
            chat_id = os.environ.get("TELEGRAM_CHAT_ID", "")
            if token and chat_id:
                sender = TelegramSender(token, chat_id)
                results["telegram"] = _test_channel("telegram", sender.send_breach_alert)

        elif ch == "email":
            alert_email = os.environ.get("ALERT_EMAIL", "")
            if alert_email and settings.smtp_username:
                sender = EmailSender(
                    to_address=alert_email,
                    smtp_host=settings.smtp_host,
                    smtp_port=settings.smtp_port,
                    smtp_username=settings.smtp_username,
                    smtp_password=settings.smtp_password,
                )
                results["email"] = _test_channel("email", sender.send_breach_alert)

        elif ch == "slack":
            webhook = os.environ.get("SLACK_WEBHOOK_URL", "")
            if webhook:
                sender = SlackSender(webhook, channel=os.environ.get("SLACK_CHANNEL"))
                results["slack"] = _test_channel("slack", sender.send_breach_alert)

        elif ch == "discord":
            webhook = os.environ.get("DISCORD_WEBHOOK_URL", "")
            if webhook:
                sender = DiscordSender(webhook)
                results["discord"] = _test_channel("discord", sender.send_breach_alert)

        elif ch == "teams":
            webhook = os.environ.get("TEAMS_WEBHOOK_URL", "")
            if webhook:
                sender = TeamsSender(webhook)
                results["teams"] = _test_channel("teams", sender.send_breach_alert)

        elif ch == "pagerduty":
            key = os.environ.get("PAGERDUTY_INTEGRATION_KEY", "")
            if key:
                sender = PagerDutySender(key, from_email=os.environ.get("ALERT_EMAIL"))
                results["pagerduty"] = _test_channel("pagerduty", sender.send_breach_alert)

        elif ch == "opsgenie":
            key = os.environ.get("OPSGENIE_API_KEY", "")
            if key:
                sender = OpsgenieSender(
                    key,
                    team=os.environ.get("OPSGENIE_TEAM"),
                    priority=os.environ.get("OPSGENIE_PRIORITY", "P2"),
                )
                results["opsgenie"] = _test_channel("opsgenie", sender.send_breach_alert)

        if ch not in results:
            results[ch] = {"success": False, "status": "not_configured"}

    # Summary
    sent = sum(1 for r in results.values() if r.get("status") == "sent")
    failed = sum(1 for r in results.values() if r.get("status") in ("failed", "error"))
    skipped = sum(1 for r in results.values() if r.get("status") == "not_configured")

    return {
        "message": f"Test complete: {sent} sent, {failed} failed, {skipped} not configured",
        "urgency": urgency,
        "results": results,
        "summary": {"sent": sent, "failed": failed, "not_configured": skipped},
        "timestamp": datetime.utcnow().isoformat(),
    }


# ========== Webhook Inspector ==========


@app.get("/webhooks/inspector/entries", tags=["webhooks"])
async def inspector_entries(
    channel: Optional[str] = Query(None, description="Filter by channel name"),
    channel_type: Optional[str] = Query(None, description="Filter by channel type"),
    success: Optional[bool] = Query(None, description="Filter by success status"),
    since: Optional[float] = Query(None, description="Unix timestamp - entries after this time"),
    limit: int = Query(100, ge=1, le=1000),
) -> Dict[str, Any]:
    """Get captured webhook payloads with optional filtering."""
    from core.alerts.inspector import get_inspector
    inspector = get_inspector()
    entries = inspector.get_entries(
        channel=channel,
        channel_type=channel_type,
        success=success,
        since=since,
        limit=limit,
    )
    return {
        "count": len(entries),
        "entries": entries,
    }


@app.get("/webhooks/inspector/entry/{entry_id}", tags=["webhooks"])
async def inspector_entry_detail(entry_id: int) -> Dict[str, Any]:
    """Get a single captured payload by ID."""
    from core.alerts.inspector import get_inspector
    inspector = get_inspector()
    entry = inspector.get_entry(entry_id)
    if entry is None:
        return {"error": f"Entry {entry_id} not found"}
    return entry


@app.get("/webhooks/inspector/stats", tags=["webhooks"])
async def inspector_stats() -> Dict[str, Any]:
    """Get inspector statistics."""
    from core.alerts.inspector import get_inspector
    return get_inspector().get_stats()


@app.get("/webhooks/inspector/channels", tags=["webhooks"])
async def inspector_channels() -> Dict[str, Any]:
    """Get list of channels that have sent payloads."""
    from core.alerts.inspector import get_inspector
    channels = get_inspector().get_channel_list()
    return {"count": len(channels), "channels": channels}


@app.post("/webhooks/inspector/capture", tags=["webhooks"])
async def inspector_capture_manual(
    channel: str = Query(..., description="Channel name"),
    channel_type: str = Query("manual", description="Channel type"),
    success: bool = Query(True, description="Success status"),
    latency_ms: int = Query(0, description="Latency in ms"),
    error: Optional[str] = Query(None, description="Error message"),
) -> Dict[str, Any]:
    """Manually capture a payload (for testing external integrations)."""
    from core.alerts.inspector import get_inspector
    inspector = get_inspector()
    entry = inspector.capture_manual(
        channel=channel,
        payload={"manual": True, "channel": channel},
        channel_type=channel_type,
        success=success,
        latency_ms=latency_ms,
        error=error,
    )
    return {"captured": True, "entry": entry}


@app.post("/webhooks/inspector/clear", tags=["webhooks"])
async def inspector_clear() -> Dict[str, Any]:
    """Clear all captured payloads."""
    from core.alerts.inspector import get_inspector
    cleared = get_inspector().clear()
    return {"cleared": cleared, "message": f"Cleared {cleared} entry(ies)"}


@app.get("/webhooks/inspector/export", tags=["webhooks"])
async def inspector_export(
    limit: int = Query(1000, ge=1, le=10000),
) -> Response:
    """Export captured payloads as JSON file."""
    from core.alerts.inspector import get_inspector
    from fastapi.responses import Response
    inspector = get_inspector()
    json_str = inspector.export_json(limit=limit)
    return Response(
        content=json_str,
        media_type="application/json",
        headers={"Content-Disposition": f"attachment; filename=webhook-inspector-export.json"},
    )


@app.post("/webhooks/inspector/toggle", tags=["webhooks"])
async def inspector_toggle(
    enabled: bool = Query(..., description="Enable or disable inspector"),
) -> Dict[str, Any]:
    """Enable or disable the webhook inspector."""
    from core.alerts.inspector import get_inspector
    inspector = get_inspector()
    inspector.enabled = enabled
    return {"enabled": enabled, "message": f"Inspector {'enabled' if enabled else 'disabled'}"}


# ========== Backup & Restore ==========


@app.get("/backup/status", tags=["backup"])
async def backup_status() -> Dict[str, Any]:
    """Show database and backup status."""
    from scripts.db_backup import show_status
    return show_status()


@app.post("/backup/run", tags=["backup"])
async def backup_run(
    output_dir: str = Query("backups"),
    compress: bool = Query(True),
    rotate: int = Query(7, ge=0, le=100),
    include_supplementary: bool = Query(False, description="Include dedup/suppression files. auth_config (secrets) is never copied via API."),
) -> Dict[str, Any]:
    """Create a database backup (HTTP-triggered output confined to the project tree)."""
    from scripts.db_backup import run_backup
    result = run_backup(output_dir=output_dir, compress=compress, rotate=rotate, include_supplementary=include_supplementary, api_mode=True)
    if result.get("status") == "error":
        raise HTTPException(status_code=400, detail=result.get("message", "Backup failed"))
    return result


@app.get("/backup/list", tags=["backup"])
async def backup_list(
    output_dir: str = Query("backups"),
) -> Dict[str, Any]:
    """List available backups (HTTP-confined)."""
    from scripts.db_backup import list_backups
    backups = list_backups(output_dir, confined=True)
    return {"count": len(backups), "backups": backups}


@app.post("/backup/verify", tags=["backup"])
async def backup_verify(
    backup_path: str = Query(...),
) -> Dict[str, Any]:
    """Verify a backup file (HTTP-confined: only agent artifacts inside the project tree)."""
    from scripts.db_backup import verify_backup
    return verify_backup(backup_path, confined=True)


# ========== Deduplication ==========


@app.get("/dedup/status", tags=["deduplication"])
async def dedup_status() -> Dict[str, Any]:
    """Get deduplication store statistics."""
    from core.dedup import get_dedup_store
    return get_dedup_store().get_stats()


@app.post("/dedup/check", tags=["deduplication"])
async def dedup_check(
    message_id: Optional[str] = Query(None),
    sender: str = Query(""),
    subject: str = Query(""),
    body: str = Query(""),
) -> Dict[str, Any]:
    """Check if a conversation would be considered a duplicate."""
    from core.dedup import get_dedup_store
    store = get_dedup_store()
    is_dup = store.is_duplicate(message_id=message_id, sender=sender, subject=subject, body=body)
    return {
        "is_duplicate": is_dup,
        "checked_at": datetime.utcnow().isoformat(),
    }


@app.post("/dedup/cleanup", tags=["deduplication"])
async def dedup_cleanup() -> Dict[str, Any]:
    """Remove expired entries from the deduplication cache."""
    from core.dedup import get_dedup_store
    store = get_dedup_store()
    removed = store.cleanup_expired()
    return {
        "removed": removed,
        "stats": store.get_stats(),
        "timestamp": datetime.utcnow().isoformat(),
    }


@app.post("/dedup/clear", tags=["deduplication"])
async def dedup_clear() -> Dict[str, Any]:
    """Clear all deduplication entries (use with caution)."""
    from core.dedup import get_dedup_store
    store = get_dedup_store()
    removed = store.clear()
    return {
        "removed": removed,
        "message": "Deduplication cache cleared",
        "timestamp": datetime.utcnow().isoformat(),
    }


# ========== Two-Factor Authentication ==========


@app.post("/auth/login", tags=["auth"])
async def auth_login(
    username: str = Form(...),
    password: str = Form(...),
    totp_code: Optional[str] = Form(None),
) -> Dict[str, Any]:
    """
    Login with username/password (+ TOTP code when 2FA is enabled).

    Returns a bearer session token usable as `Authorization: Bearer <token>`
    on every API endpoint (including the WebSocket `?api_key=` alternative).
    Brute-force protected by per-account lockout and route rate limiting.
    """
    from core.auth import get_authenticator, get_2fa_status

    auth = get_authenticator()
    ok, message, user, token = auth.authenticate_token(username, password)
    if not ok:
        raise HTTPException(status_code=401, detail=message)

    status = get_2fa_status(username)
    if status.get("enabled"):
        if not totp_code:
            raise HTTPException(status_code=401, detail="2FA code required")
        from core.auth import verify_2fa, use_backup_code
        if not verify_2fa(username, totp_code):
            backup = use_backup_code(username, totp_code)
            if not backup.get("success"):
                try:
                    from core.database.audit import audit
                    audit.log("auth:login_failed_2fa", entity_type="user", entity_id=username, details={})
                except Exception:
                    pass
                raise HTTPException(status_code=401, detail="Invalid 2FA code")

    # Session token minted by Authenticator._create_session
    if not token:
        raise HTTPException(status_code=500, detail="Session creation failed")

    try:
        from core.database.audit import audit
        audit.log("auth:login", entity_type="user", entity_id=username, details={})
    except Exception:
        pass

    return {
        "token": token,
        "token_type": "bearer",
        "expires_in_hours": auth.config.get("session_ttl_hours", 24),
        "user": user,
    }


@app.post("/auth/logout", tags=["auth"])
async def auth_logout(authorization: Optional[str] = Query(None)) -> Dict[str, Any]:
    """Invalidate the provided bearer session token."""
    from core.auth import get_authenticator
    token = (authorization or "").replace("Bearer ", "").strip()
    if not token:
        raise HTTPException(status_code=400, detail="Provide the token via ?authorization=Bearer <token>")
    get_authenticator().logout(token)
    return {"logged_out": True}


@app.post("/auth/2fa/enable", tags=["auth"])
async def auth_2fa_enable(
    username: str = Form(...),
) -> Dict[str, Any]:
    """Enable 2FA for a user. Returns secret and QR URI."""
    from core.auth import enable_2fa
    result = enable_2fa(username)
    if "error" in result:
        raise HTTPException(status_code=400, detail=result["error"])
    return result


@app.post("/auth/2fa/verify-setup", tags=["auth"])
async def auth_2fa_verify_setup(
    username: str = Form(...),
    code: str = Form(...),
) -> Dict[str, Any]:
    """Verify initial TOTP code to activate 2FA."""
    from core.auth import verify_2fa_setup
    result = verify_2fa_setup(username, code)
    if "error" in result:
        raise HTTPException(status_code=400, detail=result["error"])
    return result


@app.post("/auth/2fa/verify", tags=["auth"])
async def auth_2fa_verify(
    username: str = Form(...),
    code: str = Form(...),
) -> Dict[str, Any]:
    """Verify a 2FA code (for login)."""
    from core.auth import verify_2fa

    # Per-username throttle (route-level limiter covers per-IP)
    global _totp_verify_limiter
    try:
        _totp_verify_limiter
    except NameError:
        from core.middleware import RateLimiter
        _totp_verify_limiter = RateLimiter(max_requests=5, window_seconds=300)
    allowed, info = _totp_verify_limiter.is_allowed(username.lower())
    if not allowed:
        raise HTTPException(
            status_code=429,
            detail=f"Too many 2FA attempts for this user. Try again in {info['retry_after']}s.",
        )

    is_valid = verify_2fa(username, code)
    if not is_valid:
        try:
            from core.database.audit import audit
            audit.log("auth:2fa_failed", entity_type="user", entity_id=username, details={})
        except Exception:
            pass
    return {
        "valid": is_valid,
        "username": username,
        "timestamp": datetime.utcnow().isoformat(),
    }


@app.post("/auth/2fa/backup-code", tags=["auth"])
async def auth_2fa_backup_code(
    username: str = Form(...),
    code: str = Form(...),
) -> Dict[str, Any]:
    """Use a backup code for 2FA verification."""
    from core.auth import use_backup_code
    result = use_backup_code(username, code)
    if "error" in result:
        raise HTTPException(status_code=400, detail=result["error"])
    return result


@app.post("/auth/2fa/disable", tags=["auth"])
async def auth_2fa_disable(
    username: str = Form(...),
    password: str = Form(...),
) -> Dict[str, Any]:
    """Disable 2FA for a user (requires password confirmation)."""
    from core.auth import disable_2fa
    result = disable_2fa(username, password)
    if "error" in result:
        raise HTTPException(status_code=400, detail=result["error"])
    return result


@app.get("/auth/2fa/status/{username}", tags=["auth"])
async def auth_2fa_status(username: str) -> Dict[str, Any]:
    """Get 2FA status for a user."""
    from core.auth import get_2fa_status
    return get_2fa_status(username)


@app.get("/auth/2fa/enforcement", tags=["auth"])
async def auth_2fa_enforcement() -> Dict[str, Any]:
    """Get 2FA enforcement status for all users."""
    from core.auth import get_enforcement_status
    return get_enforcement_status()


@app.get("/auth/2fa/compliance/{username}", tags=["auth"])
async def auth_2fa_compliance(username: str) -> Dict[str, Any]:
    """Check if a user complies with 2FA enforcement."""
    from core.auth import check_2fa_compliance
    return check_2fa_compliance(username)


@app.get("/auth/2fa/non-compliant", tags=["auth"])
async def auth_2fa_non_compliant() -> Dict[str, Any]:
    """Get list of users who need to set up 2FA."""
    from core.auth import get_non_compliant_users
    users = get_non_compliant_users()
    return {
        "count": len(users),
        "users": users,
        "message": f"{len(users)} user(s) need to enable 2FA",
    }


# ========== 2FA Recovery Links ==========


@app.post("/auth/2fa/recovery/request", tags=["auth"])
async def auth_2fa_recovery_request(
    username: str = Query(...),
    send_email: bool = Query(True),
    base_url: Optional[str] = Query(None),
) -> Dict[str, Any]:
    """
    Generate and optionally email a one-time 2FA recovery link.

    SECURITY: the generated link is NEVER returned in the HTTP response.
    It is delivered exclusively to the user's registered email address.
    If SMTP is not configured, an administrator can retrieve pending
    links server-side via GET /auth/2fa/recovery/pending, or explicitly
    opt into dev-only exposure with RETURNS_LINK_IN_RESPONSE=true.
    """
    from core.auth.recovery import generate_recovery_link, send_recovery_email

    # Rate-limit recovery link generation per username (stop link spam)
    global _recovery_limiter
    try:
        _recovery_limiter
    except NameError:
        from core.middleware import RateLimiter
        _recovery_limiter = RateLimiter(max_requests=3, window_seconds=3600)
    allowed, info = _recovery_limiter.is_allowed(username)
    if not allowed:
        raise HTTPException(
            status_code=429,
            detail=f"Too many recovery requests for this user. Try again in {info['retry_after']}s.",
        )

    result = generate_recovery_link(username, base_url=base_url)
    if "error" in result:
        raise HTTPException(status_code=400, detail=result["error"])

    link = result["link"]

    if send_email:
        email_result = send_recovery_email(username, link)
        email_sent = email_result.get("success", False)
    else:
        email_result = None
        email_sent = False

    response: Dict[str, Any] = {
        "success": True,
        "username": username,
        "expires_at": result.get("expires_at"),
        "email_sent": email_sent,
        "message": (
            "Recovery link generated and emailed to the registered address."
            if email_sent
            else "Recovery link generated but SMTP is not configured. "
                 "Contact your administrator to retrieve it server-side."
        ),
    }

    # Explicit, non-default escape hatch for local development ONLY.
    if (os.environ.get("RETURNS_LINK_IN_RESPONSE", "") or "").lower() == "true":
        response["link"] = link
        response["message"] += " (dev mode: link included in response)"

    if not email_sent:
        # Server-side log so an operator can retrieve it without HTTP exposure
        logger.warning(
            f"2FA recovery link for '{username}' NOT emailed — retrieve via "
            f"GET /auth/2fa/recovery/pending from the server, or check server logs."
        )
        logger.warning(f"Recovery link (server-side only): {link}")

    return response


@app.post("/auth/2fa/recovery/redeem", tags=["auth"])
async def auth_2fa_recovery_redeem(
    token: str = Query(...),
    disable_2fa: bool = Query(True),
) -> Dict[str, Any]:
    """Redeem a one-time recovery link to reset/disable 2FA."""
    from core.auth.recovery import redeem_recovery_link

    if disable_2fa:
        result = redeem_recovery_link(token)
    else:
        # Just verify the token is valid without redeeming
        from core.auth.recovery import _hash_token, _recovery_links
        token_hash = _hash_token(token)
        link_data = _recovery_links.get(token_hash)
        if link_data is None:
            return {"error": "Invalid token", "success": False}
        if _time.time() > link_data["expires_at"]:
            return {"error": "Token expired", "success": False}
        if link_data["used"]:
            return {"error": "Token already used", "success": False}
        result = {"success": True, "valid": True, "username": link_data["username"]}

    return result


@app.get("/auth/2fa/recovery/pending", tags=["auth"])
async def auth_2fa_recovery_pending(
    username: Optional[str] = Query(None),
) -> Dict[str, Any]:
    """List pending (unused) recovery links."""
    from core.auth.recovery import get_pending_recovery_links
    links = get_pending_recovery_links(username)
    return {
        "count": len(links),
        "links": links,
    }


@app.get("/auth/2fa/recovery/stats", tags=["auth"])
async def auth_2fa_recovery_stats() -> Dict[str, Any]:
    """Get recovery link statistics."""
    from core.auth.recovery import get_recovery_stats
    return get_recovery_stats()


@app.post("/auth/2fa/recovery/cleanup", tags=["auth"])
async def auth_2fa_recovery_cleanup() -> Dict[str, Any]:
    """Clean up expired recovery links."""
    from core.auth.recovery import cleanup_expired_links
    removed = cleanup_expired_links()
    return {"removed": removed, "message": f"Cleaned up {removed} expired link(s)"}


# ========== Export & Compliance ==========


@app.get("/export/conversations/csv", tags=["export"])
async def export_conversations_csv(
    source: Optional[str] = Query(None),
    urgency: Optional[str] = Query(None),
    since: Optional[str] = Query(None),
    until: Optional[str] = Query(None),
    limit: int = Query(1000, ge=1, le=10000),
) -> Response:
    """Export conversations to CSV for compliance reporting."""
    from core.export import get_export_manager
    from fastapi.responses import FileResponse

    since_dt = datetime.fromisoformat(since) if since else None
    until_dt = datetime.fromisoformat(until) if until else None

    exporter = get_export_manager()
    path = exporter.export_conversations_csv(
        source=source, urgency=urgency,
        since=since_dt, until=until_dt, limit=limit,
    )
    return FileResponse(
        path,
        media_type="text/csv",
        filename=os.path.basename(path),
    )


@app.get("/export/conversations/pdf", tags=["export"])
async def export_conversations_pdf(
    source: Optional[str] = Query(None),
    urgency: Optional[str] = Query(None),
    since: Optional[str] = Query(None),
    until: Optional[str] = Query(None),
    limit: int = Query(100, ge=1, le=1000),
) -> Response:
    """Export conversations to PDF for compliance reporting."""
    from core.export import get_export_manager
    from fastapi.responses import FileResponse

    since_dt = datetime.fromisoformat(since) if since else None
    until_dt = datetime.fromisoformat(until) if until else None

    exporter = get_export_manager()
    path = exporter.export_conversations_pdf(
        source=source, urgency=urgency,
        since=since_dt, until=until_dt, limit=limit,
    )
    return FileResponse(
        path,
        media_type="application/pdf",
        filename=os.path.basename(path),
    )


@app.get("/export/drafts/csv", tags=["export"])
async def export_drafts_csv(
    sent_only: bool = Query(False),
    limit: int = Query(1000, ge=1, le=10000),
) -> Response:
    """Export follow-up drafts to CSV."""
    from core.export import get_export_manager
    from fastapi.responses import FileResponse

    exporter = get_export_manager()
    path = exporter.export_drafts_csv(sent_only=sent_only, limit=limit)
    return FileResponse(
        path,
        media_type="text/csv",
        filename=os.path.basename(path),
    )


@app.get("/export/audit/csv", tags=["export"])
async def export_audit_csv(
    action: Optional[str] = Query(None),
    entity_type: Optional[str] = Query(None),
    since: Optional[str] = Query(None),
    until: Optional[str] = Query(None),
    limit: int = Query(5000, ge=1, le=50000),
) -> Response:
    """Export audit trail to CSV."""
    from core.export import get_export_manager
    from fastapi.responses import FileResponse

    since_dt = datetime.fromisoformat(since) if since else None
    until_dt = datetime.fromisoformat(until) if until else None

    exporter = get_export_manager()
    path = exporter.export_audit_csv(
        action=action, entity_type=entity_type,
        since=since_dt, until=until_dt, limit=limit,
    )
    return FileResponse(
        path,
        media_type="text/csv",
        filename=os.path.basename(path),
    )


@app.get("/export/audit/pdf", tags=["export"])
async def export_audit_pdf(
    action: Optional[str] = Query(None),
    entity_type: Optional[str] = Query(None),
    since: Optional[str] = Query(None),
    until: Optional[str] = Query(None),
    limit: int = Query(500, ge=1, le=5000),
) -> Response:
    """Export audit trail to PDF."""
    from core.export import get_export_manager
    from fastapi.responses import FileResponse

    since_dt = datetime.fromisoformat(since) if since else None
    until_dt = datetime.fromisoformat(until) if until else None

    exporter = get_export_manager()
    path = exporter.export_audit_pdf(
        action=action, entity_type=entity_type,
        since=since_dt, until=until_dt, limit=limit,
    )
    return FileResponse(
        path,
        media_type="application/pdf",
        filename=os.path.basename(path),
    )


@app.get("/export/queue/csv", tags=["export"])
async def export_queue_csv() -> Response:
    """Export current queue state to CSV."""
    from core.export import get_export_manager
    from fastapi.responses import FileResponse

    exporter = get_export_manager()
    path = exporter.export_queue_csv()
    return FileResponse(
        path,
        media_type="text/csv",
        filename=os.path.basename(path),
    )


@app.get("/export/summary/pdf", tags=["export"])
async def export_summary_pdf() -> Response:
    """Export compliance summary report to PDF."""
    from core.export import get_export_manager
    from fastapi.responses import FileResponse

    exporter = get_export_manager()
    path = exporter.export_summary_pdf()
    return FileResponse(
        path,
        media_type="application/pdf",
        filename=os.path.basename(path),
    )


# Global exception handler — preserve HTTPException status codes
from fastapi.exceptions import RequestValidationError
from starlette.exceptions import HTTPException as StarletteHTTPException

@app.exception_handler(StarletteHTTPException)
async def http_exception_handler(request, exc):
    return JSONResponse(status_code=exc.status_code, content={"error": exc.detail if isinstance(exc.detail, str) else str(exc.detail), "status_code": exc.status_code})

@app.exception_handler(RequestValidationError)
async def validation_exception_handler(request, exc):
    # sanitize errors to be JSON serializable (ctx may contain ValueError)
    safe_errors = []
    for err in exc.errors():
        e = dict(err)
        if "ctx" in e and isinstance(e["ctx"], dict):
            e["ctx"] = {k: str(v) for k, v in e["ctx"].items()}
        safe_errors.append(e)
    return JSONResponse(status_code=422, content={"error": "validation_error", "details": safe_errors})

@app.exception_handler(Exception)
async def global_exception_handler(request, exc):
    # Never leak internal exception text to clients; full details only in logs
    logger.error(f"Unhandled error at {request.url.path}: {exc}", exc_info=True)
    debug = bool(getattr(settings, "debug", False))
    return JSONResponse(
        status_code=500,
        content={
            "error": "internal_server_error",
            "message": "An internal error occurred. Check the server logs." if not debug else str(exc),
        },
    )

def main():
    """Console-script entry point (`sales-followup`). Runs uvicorn."""
    import uvicorn

    run_settings = get_settings()
    auth_on = bool(run_settings.auth_required)
    configured = False
    try:
        configured = _api_keys_configured()
    except Exception:
        pass
    mode = (
        "ENFORCED (keys configured)"
        if auth_on and configured
        else "ENFORCED but NO KEYS CONFIGURED - mutating endpoints return 503"
        if auth_on
        else "DISABLED (AUTH_REQUIRED=false)"
    )
    print(
        "=" * 60 + "\n"
        "  LeadSync API\n"
        f"  http://{run_settings.host}:{run_settings.port}  |  docs: /docs\n"
        f"  API auth: {mode}\n"
        "=" * 60
    )
    uvicorn.run(
        "api.app:app",
        host=run_settings.host,
        port=run_settings.port,
        log_level="info" if not run_settings.debug else "debug",
    )


if __name__ == "__main__":
    main()
