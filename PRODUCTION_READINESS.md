# Production Readiness — LeadSync

## What was hardened (delta from introspection)

| Area | Fix |
|------|-----|
| **Meeting ingest** | `core/ingest/meeting.py:89` date regex used `group(1)` on pattern without group → `IndexError`; fixed to use `group(0)` + multi-format `strptime` loop |
| **Email ingest** | `_heuristic_extract` discarded urgency/deal; added `_heuristic_urgency_and_deal()` and wired into `fetch_emails` + `parse_email_to_conversation`; `send_email` multipart was reassigning `msg` then calling `attach` on `MIMEText` → fixed to `MIMEMultipart('alternative')` |
| **Scorer** | `recency_days` used `.days` (int) → switched to `total_seconds()/86400` fractional days |
| **LLM manager** | Cascade matching `avail.startswith(m.split(":")[0])` falsely matched `llama3.1:70b` for `llama3.1:8b` → strict exact/prefix+tag check |
| **Auth** | SHA-256+salt → `bcrypt` via `passlib[bcrypt]` with SHA fallback; default `admin123` → random `token_urlsafe(16)` unless `ADMIN_PASSWORD` set; API-key *registration* is indexed by `sha256(key)[:16]` not a 12-char prefix (collision-free), `revoke_key` scans hashes. `APIKeyVerifier.verify()` now hashes the presented key and matches via `key_hash` (bcrypt), with constant-time comparison for raw `API_KEYS` env keys — prefix matching removed |
| **TOTP** | Custom base32 decode → `base64.b32decode` with fallback |
| **Queue** | No max enforcement → `PriorityQueue(max_items=50)` evicts lowest priority non-breached; exposes `max_items` override for tests |
| **Retry** | Duplicate `RetryConfig/RetryResult` classes + `max_attempts` vs `max_retries` mismatch → unified dataclass with compat alias `max_attempts` on config |
| **Realtime** | `asyncio.get_event_loop()` deprecated → `get_running_loop()` with early return when no loop |
| **Middleware** | Rate-limit XFF bypass fixed: a client-supplied `X-Forwarded-For` is honored ONLY when the direct peer (`request.client.host`) is listed in `TRUSTED_PROXIES` (comma-separated) and passes IP validation. **`TRUSTED_PROXIES` must list your actual proxy/LB IPs**; leave it unset only when the app is exposed directly. Both the HTTP middleware (`api/app.py`) and `core/middleware` use the shared `resolve_client_ip()` helper |
| **API** | `@app.on_event` deprecated → `lifespan` context manager; global `Exception` handler swallowed `HTTPException` → split into `StarletteHTTPException`, `RequestValidationError` (sanitized `ctx` stringify), and fallback 500 with logging; `/send/follow-up` now checks `is_suppressed()` 403 + audit log; `/ingest/call` now rejects `..` and absolute paths (traversal) |
| **Config** | Added `queue_max_items_per_rep`, `trusted_proxies` |
| **Deps** | `pyproject.toml` adds `passlib[bcrypt]`, `bcrypt` |

## Tests

- Original: **498 passed**
- New simulation: **20 passed** (`tests/simulation/`)
  - `test_chaos_scenarios.py` — SLA breach storm (100), alert cooldown, LLM total/partial outage, Redis→memory fallback, GDPR flood, WS flood, brute-force lockout, dedup herd, export under mutation, metrics concurrency
  - `test_production_hardening.py` — verifies each fix is active
- Total: **518 passed** (1 legacy test `test_hash_returns_salt_and_hash` updated to accept bcrypt)

Run:

```bash
pip install -e ".[dev,database,redis,monitoring,email]"   # dev already includes bcrypt/passlib via base deps
pytest tests -q  # ~85s
pytest tests/simulation -v  # 12s critical scenarios
```

> `tests/unit/test_database.py` imports `sqlalchemy` directly, so run unit tests with at least the `dev,database` extras installed.

## Real-world simulation as test environment

`tests/simulation/` **is** the test environment — not a separate harness. It simulates IRL surrounds:

- **Load**: 100 concurrent SLA breaches, queue overflow 55+ prospects
- **Failure**: all LLM clouds + Ollama down, health demotion after 3 failures
- **Compliance**: GDPR suppression under send flood (403)
- **Abuse**: WS 30-msg/min flood, 5-attempt lockout, duplicate email thundering herd
- **Chaos**: export CSV/PDF while queue mutates via threads, Prometheus metrics under 4-thread concurrent `record_request` storm

For full chaos in Docker: `docker compose up` then `pytest tests/simulation --tb=short` hits live Redis/Postgres.

## Remaining recommendations (not yet done)

- Migrate `Conversation`/`ScoredProspect` to `ConfigDict` + `field_validator` (Pydantic V2 warnings)
- Replace `chart.googleapis.com` QR with server-side `qrcode` generation to avoid leaking secrets
- Persist `_recovery_links` and queue to Redis/DB for restart durability
- Add `fpdf2` `ln` → `new_x`/`new_y` migration
- Set `TRUSTED_PROXIES` in prod (e.g. `10.0.0.0/8` or LB IP)
