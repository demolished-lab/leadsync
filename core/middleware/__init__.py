"""
Rate limiting and authentication middleware for the Sales Follow-Up Agent.

Provides:
  - RateLimiter: sliding-window token bucket per IP/key
  - RateLimitMiddleware: HTTP middleware for FastAPI
  - WebSocketRateLimiter: per-connection rate limit for WS messages
  - ws_require_api_key: WebSocket auth dependency
"""

import time
import threading
import hmac
import os
import re
from collections import defaultdict
from datetime import datetime
from typing import Any, Callable, Dict, Optional, Tuple

from fastapi import HTTPException, Query, WebSocket, WebSocketDisconnect


def resolve_client_ip(request) -> str:
    """
    Resolve the real client IP for a request.

    A client-supplied X-Forwarded-For header is honored ONLY when the
    direct peer (request.client.host) is listed in the TRUSTED_PROXIES
    env var (comma-separated IPs). Otherwise the direct peer is used,
    which makes rate-limit identity unspoofable.
    """
    trusted_proxies = {
        p.strip() for p in (os.environ.get("TRUSTED_PROXIES", "") or "").split(",") if p.strip()
    }
    client_host = request.client.host if request.client else "unknown"

    if trusted_proxies:
        forwarded = request.headers.get("x-forwarded-for")
        if forwarded and client_host in trusted_proxies:
            candidate = forwarded.split(",")[0].strip()
            # Validate the claimed IP before trusting it
            if re.match(r"^\d{1,3}(\.\d{1,3}){3}$", candidate) or ":" in candidate:
                return candidate
    return client_host


# ── Sliding Window Rate Limiter ────────────────────────────────


class RateLimiter:
    """
    Sliding-window rate limiter.

    Tracks request timestamps per key (IP, user ID, etc.) and rejects
    requests that exceed the configured limit within the window.

    Thread-safe via a lock.
    """

    def __init__(
        self,
        max_requests: int = 60,
        window_seconds: int = 60,
    ):
        """
        Args:
            max_requests: Max requests allowed per window.
            window_seconds: Window duration in seconds.
        """
        self.max_requests = max_requests
        self.window_seconds = window_seconds
        self._requests: Dict[str, list] = defaultdict(list)
        self._lock = threading.Lock()

    def is_allowed(self, key: str) -> Tuple[bool, Dict[str, Any]]:
        """
        Check if a request is allowed for the given key.

        Returns:
            (allowed, info_dict)
            info_dict contains: limit, remaining, reset_at, retry_after
        """
        now = time.time()
        cutoff = now - self.window_seconds

        with self._lock:
            # Clean old entries
            timestamps = self._requests[key]
            self._requests[key] = [t for t in timestamps if t > cutoff]

            count = len(self._requests[key])
            reset_at = cutoff + self.window_seconds

            if count >= self.max_requests:
                oldest = self._requests[key][0] if self._requests[key] else now
                retry_after = oldest + self.window_seconds - now
                return False, {
                    "limit": self.max_requests,
                    "remaining": 0,
                    "reset_at": datetime.fromtimestamp(reset_at).isoformat(),
                    "retry_after": max(1, int(retry_after)),
                }

            # Allow — record this request
            self._requests[key].append(now)
            remaining = self.max_requests - count - 1
            return True, {
                "limit": self.max_requests,
                "remaining": remaining,
                "reset_at": datetime.fromtimestamp(reset_at).isoformat(),
                "retry_after": 0,
            }

    def get_usage(self, key: str) -> Dict[str, Any]:
        """Get current usage for a key without recording a request."""
        now = time.time()
        cutoff = now - self.window_seconds
        with self._lock:
            timestamps = self._requests.get(key, [])
            active = [t for t in timestamps if t > cutoff]
            return {
                "key": key,
                "requests_in_window": len(active),
                "limit": self.max_requests,
                "window_seconds": self.window_seconds,
            }

    def reset(self, key: Optional[str] = None) -> None:
        """Reset counters for a specific key or all keys."""
        with self._lock:
            if key:
                self._requests.pop(key, None)
            else:
                self._requests.clear()

    def cleanup(self) -> int:
        """Remove expired entries. Returns number of keys cleaned."""
        now = time.time()
        cutoff = now - self.window_seconds
        cleaned = 0
        with self._lock:
            keys_to_delete = []
            for key, timestamps in self._requests.items():
                self._requests[key] = [t for t in timestamps if t > cutoff]
                if not self._requests[key]:
                    keys_to_delete.append(key)
            for key in keys_to_delete:
                del self._requests[key]
                cleaned += 1
        return cleaned


# ── HTTP Rate Limit Middleware ──────────────────────────────────


class RateLimitMiddleware:
    """
    FastAPI middleware that applies rate limiting per client IP.

    Configurable per-route limits via route_settings dict.
    """

    def __init__(
        self,
        default_max_requests: int = 120,
        default_window_seconds: int = 60,
        route_settings: Optional[Dict[str, Dict[str, int]]] = None,
        excluded_paths: Optional[list] = None,
    ):
        """
        Args:
            default_max_requests: Default request limit.
            default_window_seconds: Default window duration.
            route_settings: Per-path overrides, e.g.:
                {"/pipeline/process": {"max_requests": 10, "window_seconds": 60}}
            excluded_paths: Paths to skip rate limiting for.
        """
        self._limiters: Dict[str, RateLimiter] = {}
        self._default = RateLimiter(default_max_requests, default_window_seconds)
        self._route_settings = route_settings or {}
        self._excluded = set(excluded_paths or ["/health", "/metrics", "/docs", "/openapi.json"])
        self._ip_limiters: Dict[str, RateLimiter] = {}

    def _get_limiter(self, path: str) -> RateLimiter:
        """Get or create a rate limiter for a specific path."""
        if path in self._route_settings:
            if path not in self._limiters:
                cfg = self._route_settings[path]
                self._limiters[path] = RateLimiter(
                    cfg.get("max_requests", 60),
                    cfg.get("window_seconds", 60),
                )
            return self._limiters[path]
        return self._default

    def _get_client_ip(self, request) -> str:
        """Extract client IP — only trust X-Forwarded-For if from trusted proxy."""
        return resolve_client_ip(request)

    def check(self, path: str, client_ip: str) -> Tuple[bool, Dict[str, Any]]:
        """Check if a request is allowed."""
        if path in self._excluded:
            return True, {}
        limiter = self._get_limiter(path)
        return limiter.is_allowed(client_ip)

    def get_all_usage(self) -> Dict[str, Any]:
        """Get usage stats for all tracked keys."""
        result = {}
        for key, limiter in self._ip_limiters.items():
            result[key] = limiter.get_usage(key)
        return result


# ── WebSocket Rate Limiter ─────────────────────────────────────


class WebSocketRateLimiter:
    """
    Per-connection rate limiter for WebSocket messages.

    Limits how many messages a single WebSocket client can send
    within a time window. Does NOT limit received broadcasts.
    """

    def __init__(
        self,
        max_messages: int = 30,
        window_seconds: int = 60,
    ):
        self.max_messages = max_messages
        self.window_seconds = window_seconds
        self._connections: Dict[str, list] = defaultdict(list)
        self._lock = threading.Lock()

    def _client_key(self, websocket) -> str:
        """Generate a key for a WebSocket connection."""
        client = websocket.client
        if client:
            return f"{client.host}:{client.port}"
        return "unknown"

    def is_allowed(self, websocket) -> Tuple[bool, Dict[str, Any]]:
        """Check if a message from this WebSocket is allowed."""
        key = self._client_key(websocket)
        now = time.time()
        cutoff = now - self.window_seconds

        with self._lock:
            timestamps = self._connections[key]
            self._connections[key] = [t for t in timestamps if t > cutoff]
            count = len(self._connections[key])

            if count >= self.max_messages:
                oldest = self._connections[key][0] if self._connections[key] else now
                retry_after = oldest + self.window_seconds - now
                return False, {
                    "limit": self.max_messages,
                    "remaining": 0,
                    "retry_after": max(1, int(retry_after)),
                }

            self._connections[key].append(now)
            return True, {
                "limit": self.max_messages,
                "remaining": self.max_messages - count - 1,
            }

    def remove(self, websocket) -> None:
        """Clean up when a WebSocket disconnects."""
        key = self._client_key(websocket)
        with self._lock:
            self._connections.pop(key, None)


# ── API Key Authentication ─────────────────────────────────────


class APIKeyVerifier:
    """
    Verify API keys for WebSocket and HTTP access.

    Keys are loaded from the auth config. Supports both full keys
    and key prefixes for lookup.
    """

    def __init__(self):
        self._cache: Optional[Dict] = None
        self._cache_time: float = 0
        self._cache_ttl: float = 30  # seconds

    def _load_keys(self) -> Dict[str, Any]:
        """Load API keys from auth config with caching."""
        now = time.time()
        if self._cache is not None and (now - self._cache_time) < self._cache_ttl:
            return self._cache

        try:
            from core.auth import _load_config
            config = _load_config()
            self._cache = config.get("api_keys", {})
            self._cache_time = now
        except Exception:
            self._cache = {}

        return self._cache

    def verify(self, api_key: str) -> Optional[Dict[str, Any]]:
        """
        Verify an API key. Returns key metadata or None if invalid.

        Stored entries are keyed by sha256(key)[:16] with the full key kept
        as a bcrypt/SHA hash in ``key_hash`` — so matching is done by hashing
        the presented key, never by prefix comparison.
        Also accepts raw keys passed via the API_KEYS env var (comma-separated).
        """
        if not api_key:
            return None

        keys = self._load_keys()
        for key_data in keys.values():
            if not isinstance(key_data, dict):
                continue
            try:
                from core.auth import verify_password
                if verify_password(api_key, key_data.get("key_hash", "")):
                    return {
                        "name": key_data.get("name", "unknown"),
                        "role": key_data.get("role", "rep"),
                    }
            except Exception:
                pass

        # Env-var fallback: API_KEYS="key1,key2" — compare in constant time
        env_keys = (os.environ.get("API_KEYS") or "").split(",")
        for candidate in env_keys:
            candidate = candidate.strip()
            if candidate and hmac.compare_digest(api_key, candidate):
                return {"name": "env-key", "role": "rep"}

        return None

    def invalidate_cache(self) -> None:
        """Force reload of keys on next verification."""
        self._cache = None


# Global instances
_api_key_verifier: Optional[APIKeyVerifier] = None


def get_api_key_verifier() -> APIKeyVerifier:
    global _api_key_verifier
    if _api_key_verifier is None:
        _api_key_verifier = APIKeyVerifier()
    return _api_key_verifier


# ── WebSocket Auth Dependency ───────────────────────────────────


async def ws_require_api_key(
    websocket: WebSocket,
    api_key: Optional[str] = Query(None),
) -> Dict[str, Any]:
    """
    FastAPI dependency for WebSocket API key authentication.

    Accepts the API key as a query parameter: ws://host/ws/queue?api_key=xxx

    Returns user metadata dict on success.
    Raises WebSocket close(4001) on failure.
    """
    verifier = get_api_key_verifier()

    if not api_key:
        # Also check for token in first message as fallback
        try:
            import asyncio
            data = await asyncio.wait_for(websocket.receive_text(), timeout=5.0)
            # Try to parse as JSON with api_key field
            import json
            try:
                msg = json.loads(data)
                api_key = msg.get("api_key", "")
            except (json.JSONDecodeError, AttributeError):
                api_key = ""
        except Exception:
            api_key = ""

    if not api_key:
        await websocket.close(code=4001, reason="API key required")
        raise WebSocketDisconnect(code=4001)

    metadata = verifier.verify(api_key)
    if metadata is None:
        await websocket.close(code=4003, reason="Invalid API key")
        raise WebSocketDisconnect(code=4003)

    return metadata
