# ════════════════════════════════════════════════════════════════
# AI Sales Follow-Up Agent — Production Dockerfile
# ════════════════════════════════════════════════════════════════
# Multi-stage build for minimal image size.
# Includes ffmpeg for call recording transcription (STT).

# ── Stage 1: Build dependencies ───────────────────────────────
FROM python:3.12-slim AS builder

WORKDIR /build

# System deps for building Python packages
RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    && rm -rf /var/lib/apt/lists/*

COPY pyproject.toml README.md ./
COPY core/ core/
COPY api/ api/

# Install into a virtual env for clean copying.
# Extras: database (sqlalchemy/psycopg2), redis, monitoring, email are the
# minimal set the container actually runs with. llm/stt stay optional —
# the stt extra pulls openai-whisper, which is a multi-GB download.
RUN python -m venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"
RUN pip install --no-cache-dir --upgrade pip \
    && pip install --no-cache-dir ".[database,redis,monitoring,email]"

# ── Stage 2: Runtime image ────────────────────────────────────
FROM python:3.12-slim AS runtime

# ffmpeg for audio preprocessing in STT pipeline
RUN apt-get update && apt-get install -y --no-install-recommends \
    ffmpeg \
    curl \
    && rm -rf /var/lib/apt/lists/*

# Copy virtual env from builder
COPY --from=builder /opt/venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"
ENV PYTHONUNBUFFERED=1
ENV PYTHONDONTWRITEBYTECODE=1

WORKDIR /app

# Copy application code
COPY api/ api/
COPY core/ core/
COPY ui/ ui/
COPY pyproject.toml ./
COPY docker/entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh

# Create non-root user; /app and data dirs owned by it (volumes such as
# suppressions:/app/data/suppressions need a writable, app-owned parent)
RUN groupadd -r app && useradd -r -g app -d /app -s /bin/bash app \
    && mkdir -p /app/data/suppressions \
    && chown -R app:app /app
USER app

# Expose ports
# 8000: FastAPI API
# 8501: Streamlit dashboard
EXPOSE 8000 8501

# Health check — hits the /health endpoint
HEALTHCHECK --interval=30s --timeout=10s --start-period=20s --retries=3 \
    CMD curl -f http://localhost:8000/health || exit 1

ENTRYPOINT ["/entrypoint.sh"]
CMD ["api"]
