# LeadSync

<p align="center">
  <strong>Autonomous AI-Powered Sales Follow-Up Platform</strong><br>
  <em>Never lose a lead to silence again.</em>
</p>

<p align="center">
  <strong>Hackathon Project</strong><br>
  Problem statement by <a href="https://in.linkedin.com/company/theproductspace">Product Space</a> via Unstop<br>
  <a href="https://in.linkedin.com/company/theproductspace">
    <img src="https://img.shields.io/badge/Problem%20Statement-Product%20Space-blue?style=flat-square&logo=linkedin" alt="Product Space">
  </a>
  <a href="https://unstop.com">
    <img src="https://img.shields.io/badge/Platform-Unstop-orange?style=flat-square&logo=unstop" alt="Unstop">
  </a>
</p>

---

## What is LeadSync?

LeadSync is a fully autonomous sales follow-up pipeline that ingests conversations from email, phone calls, and meetings, scores them using weighted AI models, and generates personalized follow-up drafts — all without human intervention.

Built for sales teams who refuse to let promising leads go cold.

### The Problem

Sales reps lose **48% of their productive time** to manual follow-up work: checking inboxes, drafting emails, prioritizing leads, and tracking responses. Meanwhile, leads go silent, SLAs breach, and deals die in the pipeline.

### The Solution

LeadSync automates the entire follow-up lifecycle:

```
┌─────────────┐     ┌──────────────┐     ┌────────────────┐     ┌──────────────┐
│  Ingestion  │────▶│ Intelligence │────▶│   Generation   │────▶│   Delivery   │
│  (Email /   │     │ (Score +     │     │ (3 tone        │     │ (Email with  │
│   STT /     │     │  Action)     │     │  variants)     │     │  tracking)   │
│   Meeting)  │     │              │     │                │     │              │
└─────────────┘     └──────────────┘     └────────────────┘     └──────────────┘
       │                   │                     │                     │
       ▼                   ▼                     ▼                     ▼
  ┌─────────┐      ┌─────────────┐      ┌──────────────┐      ┌──────────┐
  │ IMAP /  │      │  Priority   │      │  LLM Manager │      │  Email   │
  │ Whisper │      │  Queue +    │      │  (Cloud +    │      │ Tracking │
  │ / Notes │      │  SLA Track  │      │   Local)     │      │  Pixels  │
  └─────────┘      └─────────────┘      └──────────────┘      └──────────┘
```

---

## Key Features

### Autonomous Pipeline

A single API call processes a conversation end-to-end. No manual steps required.

```bash
POST /pipeline/process
# → Score → Determine Action → Generate Drafts → Queue → Track
```

### Intelligent LLM Fallback

LeadSync never fails silently. When one AI provider goes down, it automatically falls back:

```
Configured Provider → OpenAI → Anthropic → Google → Groq → NVIDIA NIM → Ollama (Local)
```

- Automatic health tracking per provider
- Dynamic model discovery from local Ollama
- Cost and latency monitoring per request
- Zero-downtime model switching

### Real Implementations (No Stubs)

Every component uses production-grade code:

| Component | Implementation |
|-----------|---------------|
| Email Ingestion | IMAP fetching with LLM entity extraction |
| Speech-to-Text | Whisper transcription with ffmpeg preprocessing |
| Meeting Notes | NLP extraction of commitments, urgency, sentiment |
| Priority Scoring | Weighted formula: urgency (40%), deal value (30%), engagement (20%), recency (10%) |
| Action Engine | Determines close / re-engage / nurture / escalate based on score + context |
| Draft Generation | 3 tone variants (agreeable, direct, soft) via LLM with deterministic fallback |
| Email Tracking | Open/click pixels that **record real events** (memory + audit trail + DB counters) with engagement scoring |
| Suppression List | File-based GDPR/CCPA compliance |

### Multi-Channel Alerting (7 Channels)

When SLA breaches occur, LeadSync notifies through every configured channel simultaneously:

| Channel | Protocol | Features |
|---------|----------|----------|
| Telegram | Bot API | HTML formatting, escalation warnings |
| Email | SMTP | Full HTML email with tracking |
| Slack | Incoming Webhook | Color-coded attachments |
| Discord | Webhook | Embed with inline fields |
| Teams | Office 365 Connector | MessageCard with theme color |
| PagerDuty | Events API v2 | Incident creation + resolution |
| Opsgenie | Alert API v2 | Priority routing + team escalation |

### 2FA Security

- TOTP-based two-factor authentication (Google Authenticator compatible)
- Backup codes for account recovery
- Recovery email with one-time use links
- Mandatory 2FA enforcement for admin accounts
- Role-based access control (admin, rep, viewer)

### API Authentication (fail-closed by default)

Every mutating/admin endpoint requires an API key or bearer session token unless you explicitly disable auth for local development:

| Variable | Default | Behavior |
|----------|---------|----------|
| `AUTH_REQUIRED` | `true` | Enforce API-key auth on protected endpoints |
| `API_KEYS` | *(empty)* | Comma-separated raw keys (hashed internally, compared in constant time) |
| `ADMIN_PASSWORD` | *(random)* | Bootstrap admin password; if unset a random one is generated and written to a `0600` bootstrap file — never logged |

```bash
# Create a key instead of using env vars
python -m core.auth create-key my-integration --role admin

# Call the API
curl -H "X-API-Key: <key>" http://localhost:8000/queue/stats -X POST
# ... or with a session token from POST /auth/login
curl -H "Authorization: Bearer <token>" ...
```

**Fail-closed behavior:** with `AUTH_REQUIRED=true` and no keys configured, protected endpoints return `503 api_auth_not_configured` with remediation instructions — LeadSync never silently runs unauthenticated. Session tokens are issued by `POST /auth/login` (username/password + TOTP when 2FA is enabled) and invalidated by `POST /auth/logout`.

Public endpoints: `/health`, `/metrics`, `/docs`, tracking pixel/click callbacks (called by email clients), OAuth callback, and one-time recovery redemption.

**Other security defaults:**

- **Recovery links are never returned in HTTP responses** — they are emailed to the registered address only. (Dev-only escape hatch: `RETURNS_LINK_IN_RESPONSE=true`.)
- Per-user throttling on 2FA verification, login, and recovery-link requests.
- Rate-limit identity only honors `X-Forwarded-For` from proxies listed in `TRUSTED_PROXIES` (comma-separated IPs) — the header can't be spoofed to defeat the limiter.
- Google OAuth uses single-use `state` tokens (CSRF); the Gmail token file is written `0600`.
- Backup endpoints confine paths to the project tree (no `.env` reads via `/backup/verify`) and never copy `auth_config.yaml` via HTTP-triggered backups.
- Caller-supplied IMAP hosts are refused when they resolve to private IPs (SSRF guard; override with `ALLOW_PRIVATE_IMAP=true` for self-hosted mail).
- CORS is an explicit allowlist (`ALLOWED_ORIGINS`), defaulting to the local Streamlit UI.
- CSV/PDF exports neutralize spreadsheet formula injection and unsupported unicode.
- `/ingest/call` accepts multipart audio uploads (50 MB limit, extension allowlist, virus-safe temp-dir cleanup) instead of taking arbitrary server paths.

### Webhook Payload Inspector

Debug exactly what gets sent to each service:

- Captures outgoing payloads before delivery
- Filterable by channel, status, time range
- Full JSON payload inspection
- Latency and error tracking
- Export to JSON

### Real-Time Updates

WebSocket-based live updates when queue changes:

- Queue additions, removals, and pops
- SLA breach notifications
- Connection status and rate limiting
- API key authentication

### Database Persistence

- SQLite out of the box; PostgreSQL/SQLite both work via `DATABASE_URL`
- Alembic migrations (`alembic upgrade head` or `python scripts/migrate.py`)
- Every ingested conversation, scored prospect, and draft is persisted automatically (best-effort — the pipeline never breaks on DB errors)
- Conversation history and immutable audit trail
- Automated backup, verify, restore and rotation (with path confinement over HTTP)
- CSV/PDF export for compliance

### Compliance

- GDPR and CCPA compliant
- Email suppression list management
- Conversation deduplication
- Audit trail for all actions

---

## Architecture

### Tech Stack

| Layer | Technology |
|-------|-----------|
| **API** | FastAPI (Python 3.10+) |
| **Dashboard** | Streamlit |
| **Queue** | In-Memory or Redis |
| **Database** | PostgreSQL or SQLite |
| **LLM** | OpenAI, Anthropic, Google, Groq, NVIDIA NIM, Ollama |
| **Monitoring** | Prometheus + Grafana |
| **Auth** | Session-based + API keys + TOTP 2FA |
| **Real-time** | WebSocket |

### Project Structure

```
leadsync/
├── api/                    # FastAPI application
│   └── app.py              # 40+ REST endpoints + WebSocket
├── core/
│   ├── alerts/             # 7-channel alerting + retry + inspector
│   ├── auth/               # Authentication, 2FA, recovery
│   ├── database/           # PostgreSQL/SQLite models + audit
│   ├── dedup/              # Conversation deduplication
│   ├── export/             # CSV/PDF export
│   ├── generation/         # LLM draft generation
│   ├── ingest/             # Email, STT, meeting ingestion
│   ├── intelligence/       # Scoring, action engine, LLM manager
│   ├── middleware/          # Rate limiting, API key auth
│   ├── monitoring/         # Prometheus metrics
│   ├── queue/              # Priority queue (memory/Redis)
│   └── realtime/           # WebSocket event bus
├── ui/streamlit/           # Streamlit dashboard
├── scripts/                # DB init, migrations, backup
├── tests/                  # 490 unit + integration tests
├── docker/                 # Docker + Prometheus + Grafana
├── alembic/                # Database migrations
└── pyproject.toml          # Project configuration
```

---

## Quick Start

### Prerequisites

- Python 3.10+
- Redis (optional, for production queue)
- PostgreSQL (optional, for production database)
- Ollama (optional, for local LLM)

### Installation

```bash
# Clone
git clone https://github.com/demolished-lab/leadsync.git
cd leadsync
pip install -e .
```

### Running — one command

```bash
python main.py
# → API http://localhost:8000/docs + UI http://localhost:8501 auto-start
# All Gmail / LLM / Air-gapped setup via Web UI: Settings (no .env editing needed)
```

**Manual (alternative):**
```bash
uvicorn api.app:app --host 0.0.0.0 --port 8000
streamlit run ui/streamlit/app.py  # second terminal
```

### Docker

```bash
cd docker
cp ../.env.example ../.env
docker compose up -d

# View logs
docker compose logs -f api

# Stop
docker compose down
```

---

## API Reference

### Core Endpoints

| Method | Endpoint | Description |
|--------|----------|-------------|
| `GET` | `/health` | Health check + LLM status *(public)* |
| `POST` | `/pipeline/process` | **Full autonomous pipeline** |
| `POST` | `/ingest/emails` | Fetch emails from IMAP |
| `POST` | `/ingest/email` | Parse single email |
| `POST` | `/ingest/call` | Transcribe call recording (multipart upload or on-disk path) |
| `POST` | `/ingest/meeting` | Parse meeting notes |
| `POST` | `/score` | Score a conversation |
| `POST` | `/action/determine` | Get next best action |
| `POST` | `/drafts/generate` | Generate 3 tone variants |
| `GET` | `/tracking/pixel/{id}` | Open-tracking pixel (records the event) *(public)* |
| `POST` | `/tracking/click` | Click tracking (records the event, updates the queue) |
| `GET` | `/tracking/engagement/{id}` | Open/click stats + engagement score |

### Queue Management

| Method | Endpoint | Description |
|--------|----------|-------------|
| `GET` | `/queue/stats` | Queue statistics |
| `GET` | `/queue/list` | List queued prospects |
| `POST` | `/queue/pop` | Pop next prospect |
| `WS` | `/ws/queue` | WebSocket real-time events |

### Alerting & Monitoring

| Method | Endpoint | Description |
|--------|----------|-------------|
| `GET` | `/webhooks/channels` | List configured channels |
| `POST` | `/webhooks/test` | Send test alert |
| `GET` | `/webhooks/inspector/stats` | Payload inspector stats |
| `GET` | `/webhooks/inspector/entries` | Captured payloads |
| `GET` | `/metrics` | Prometheus metrics |

### Security

All endpoints in the Reference (except the ones marked *public*) require `X-API-Key` or a bearer token (see [API Authentication](#api-authentication-fail-closed-by-default)).

| Method | Endpoint | Description |
|--------|----------|-------------|
| `POST` | `/auth/login` | Authenticate user → bearer session token |
| `POST` | `/auth/logout` | Invalidate a session token |
| `POST` | `/auth/2fa/enable` | Enable 2FA |
| `POST` | `/auth/2fa/verify` | Verify TOTP code (throttled per user) |
| `POST` | `/auth/2fa/recovery/request` | Request recovery link — **emailed only, never in the response** |

### Export, Backup & Compliance

> Backup endpoints confine paths to the project tree and exclude `auth_config.yaml` (secrets) from HTTP-triggered backups. CLI runs (`python scripts/db_backup.py ...`) keep full flexibility for external destinations.

| Method | Endpoint | Description |
|--------|----------|-------------|
| `GET` | `/export/conversations/csv` | Export conversations |
| `GET` | `/export/audit/pdf` | Export audit trail |
| `GET` | `/backup/status` | Database backup status |
| `POST` | `/backup/run` | Create backup |

---

## Configuration

### Environment Variables

```env
# LLM Provider (ollama, openai, anthropic, google, groq, nim)
LLM_PROVIDER=ollama
OPENAI_API_KEY=sk-...
OLLAMA_HOST=http://localhost:11434

# Email
IMAP_HOST=imap.gmail.com
SMTP_HOST=smtp.gmail.com
SMTP_USERNAME=you@gmail.com
SMTP_PASSWORD=app-specific-password

# SLA Timers (hours)
SLA_HIGH_HOURS=24
SLA_MEDIUM_HOURS=48
SLA_LOW_HOURS=72

# Redis (optional)
USE_REDIS=true
REDIS_URL=redis://localhost:6379

# Database — SQLite by default (out of the box), Postgres for production
DATABASE_URL=sqlite:///./data/leadsync.db
# DATABASE_URL=postgresql://user:pass@localhost:5432/leadsync

# Security
AUTH_REQUIRED=true
API_KEYS=key-for-scripts-1,key-for-ci-2        # empty + AUTH_REQUIRED=true → protected endpoints return 503
ADMIN_PASSWORD=                                # random one-time bootstrap password if empty
ENFORCE_2FA_ADMIN=true
RECOVERY_LINK_TTL_HOURS=1
TRUSTED_PROXIES=                               # list your reverse-proxy IPs only, e.g. 10.0.0.5,10.0.0.6
ALLOWED_ORIGINS=                               # CORS allowlist; default: local Streamlit UI
API_BASE_URL=http://localhost:8000             # used by the Streamlit UI

# Alerting
TELEGRAM_BOT_TOKEN=your-bot-token
TELEGRAM_CHAT_ID=your-chat-id
SLACK_WEBHOOK_URL=https://hooks.slack.com/...
DISCORD_WEBHOOK_URL=https://discord.com/api/webhooks/...
```

### LLM Fallback Chain

The LLM Manager automatically routes through providers in order:

1. **Configured provider** (from `LLM_PROVIDER`)
2. **Alternative cloud providers** (any with API keys configured)
3. **Local Ollama** (auto-discovers available models)

Health tracking ensures broken providers are skipped automatically.

---

## Dashboard

The Streamlit dashboard provides a rich UI for sales teams:

| Page | Description |
|------|-------------|
| **📊 Dashboard** | Queue overview, SLA alerts, top prospects |
| **📥 Process** | Enter conversations, see AI-generated drafts |
| **📋 Queue** | Filter, sort, manage queued prospects |
| **🤖 LLM Status** | Provider health, model availability |
| **👥 Users** | User management, 2FA setup, API keys |
| **🔍 Inspector** | Webhook payload debugging |
| **🔌 WebSocket** | Real-time event testing |

```bash
streamlit run ui/streamlit/app.py
```

---

## Testing

```bash
# Run all 490 tests
pytest tests/ -v

# Unit tests only
pytest tests/unit/ -v

# Specific module
pytest tests/unit/test_scorer.py -v

# With coverage
pytest tests/ --cov=core --cov-report=html
```

### Test Coverage

| Module | Tests |
|--------|-------|
| Auth & 2FA | 77 |
| Alerts & Channels | 47 |
| Webhook Inspector | 40 |
| Recovery Links | 27 |
| Deduplication | 27 |
| Middleware | 23 |
| Backup & Restore | 19 |
| Export | 15 |
| Queue | 12 |
| Config | 10 |
| Database | 8 |
| Realtime | 8 |
| Models | 6 |
| Scoring | 5 |
| ... | ... |

---

## Deployment

### Docker Compose Services

| Service | Port | Description |
|---------|------|-------------|
| API | 8000 | FastAPI backend |
| Dashboard | 8501 | Streamlit UI |
| Redis | 6379 | Queue backend (AOF persistence) |
| PostgreSQL | 5432 | Production database |
| Prometheus | 9090 | Metrics collection |
| Grafana | 3000 | Metrics dashboards |

### Prometheus Metrics

The API exposes metrics at `/metrics`:

- `leadsync_http_requests_total` — Request counts by method/path/status
- `leadsync_http_request_duration_seconds` — Request latency histogram
- `leadsync_queue_depth` — Current queue size
- `leadsync_sla_breaches` — Active SLA breaches
- `leadsync_uptime_seconds` — Process uptime

---

## Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md) for guidelines.

1. Fork the repository
2. Create a feature branch (`git checkout -b feature/amazing-feature`)
3. Commit your changes (`git commit -m 'Add amazing feature'`)
4. Push to the branch (`git push origin feature/amazing-feature`)
5. Open a Pull Request

---

## License

This project is licensed under the MIT License — see the [LICENSE](LICENSE) file for details.

---

## Credits

This project was built as part of a hackathon, solving the problem statement provided by:

- **[Product Space](https://in.linkedin.com/company/theproductspace)** — Product management community and learning platform
- **[Unstop](https://unstop.com)** — Platform hosting the hackathon challenge

We thank Product Space for the inspiring problem statement on autonomous sales follow-up systems.

---

## Support

- **Issues**: [GitHub Issues](https://github.com/demolished-lab/leadsync/issues)
- **Discussions**: [GitHub Discussions](https://github.com/demolished-lab/leadsync/discussions)
