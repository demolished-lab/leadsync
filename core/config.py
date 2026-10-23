"""Application configuration loaded from environment variables."""

import os
from typing import Dict, Any, Optional

try:
    from pydantic_settings import BaseSettings

    class Settings(BaseSettings):
        """Application configuration with env var loading."""

        # LLM Configuration
        llm_provider: str = "ollama"
        openai_api_key: Optional[str] = None
        anthropic_api_key: Optional[str] = None
        google_api_key: Optional[str] = None
        nvidia_api_key: Optional[str] = None
        groq_api_key: Optional[str] = None
        llm_model_generation: str = "gpt-4o-mini"
        llm_model_scoring: str = "gpt-4o-mini"
        llm_temperature: float = 0.7
        llm_max_tokens: int = 500

        # Ollama / Local
        ollama_host: str = "http://localhost:11434"
        ollama_model_primary: str = "llama3.1:8b"
        ollama_model_fallback: str = "llama3.2:1b"

        # Email Configuration
        imap_host: str = "imap.gmail.com"
        imap_port: int = 993
        imap_username: Optional[str] = None
        imap_password: Optional[str] = None
        smtp_host: str = "smtp.gmail.com"
        smtp_port: int = 587
        smtp_username: Optional[str] = None
        smtp_password: Optional[str] = None
        email_sending_domain: str = "sales@yourcompany.com"
        email_tracking_enabled: bool = True

        # SLA & Queue
        sla_high_urgency_hours: int = 24
        sla_medium_urgency_hours: int = 48
        sla_low_urgency_hours: int = 72
        queue_max_items_per_rep: int = 50
        priority_score_decay_days: float = 7.0

        # Model / STT
        whisper_model_size: str = "base"

        # Compliance
        suppressions_list_path: Optional[str] = None
        gdpr_compliance_enabled: bool = True
        ccpa_compliance_enabled: bool = True

        # Server
        host: str = "0.0.0.0"
        port: int = 8000
        debug: bool = False

        # Pipeline Integration
        pipeline_enabled: bool = True

        # Monitoring
        prometheus_enabled: bool = True
        metrics_port: int = 9090

        # Tracking base URL (must be publicly reachable for tracking pixels)
        tracking_base_url: str = "http://localhost:8000/track"

        # Security
        # Comma-separated raw API keys (hashed internally). Leave empty to
        # disable API-key auth entirely (see auth_required).
        api_keys: str = ""
        # Comma-separated CORS allowlist. Empty → localhost UI origins only.
        allowed_origins: str = ""
        # When true, all mutating/admin API endpoints require a valid API key.
        auth_required: bool = True
        # Only honor X-Forwarded-For from these proxy IPs/CIDR-ish prefixes
        trusted_proxies: str = ""
        # Base URL of the FastAPI API as reachable from the Streamlit UI
        api_base_url: str = "http://localhost:8000"
        # Capture outbound webhook payloads in the inspector
        inspector_enabled: bool = True

        # Gmail OAuth (like Macro — Sign in with Google)
        google_client_id: Optional[str] = None
        google_client_secret: Optional[str] = None
        google_redirect_uri: str = "http://localhost:8000/auth/google/callback"
        gmail_token_path: str = "data/gmail_token.json"

        # Redis (optional)
        use_redis: bool = False
        redis_url: str = "redis://localhost:6379"

        # Database (optional)
        database_url: Optional[str] = None
        # Air-gapped / offline mode — when true, no external network calls are made
        air_gapped: bool = False

        # Security
        enforce_2fa_admin: bool = False
        enforce_2fa_rep: bool = False

        # 2FA Recovery
        recovery_link_ttl_hours: int = 1
        recovery_max_links: int = 3
        recovery_base_url: str = "http://localhost:8000"

        model_config = {"env_file": ".env", "env_file_encoding": "utf-8", "extra": "ignore"}

except ImportError:
    # Fallback without pydantic-settings
    class Settings:
        """Application configuration loaded from os.environ."""

        def __init__(self):
            self.llm_provider: str = os.environ.get("LLM_PROVIDER", "ollama")
            self.openai_api_key: Optional[str] = os.environ.get("OPENAI_API_KEY")
            self.anthropic_api_key: Optional[str] = os.environ.get("ANTHROPIC_API_KEY")
            self.google_api_key: Optional[str] = os.environ.get("GOOGLE_API_KEY")
            self.nvidia_api_key: Optional[str] = os.environ.get("NVIDIA_API_KEY")
            self.groq_api_key: Optional[str] = os.environ.get("GROQ_API_KEY")
            self.llm_model_generation: str = os.environ.get("LLM_MODEL_GENERATION", "gpt-4o-mini")
            self.llm_model_scoring: str = os.environ.get("LLM_MODEL_SCORING", "gpt-4o-mini")
            self.llm_temperature: float = float(os.environ.get("LLM_TEMPERATURE", "0.7"))
            self.llm_max_tokens: int = int(os.environ.get("LLM_MAX_TOKENS", "500"))

            self.ollama_host: str = os.environ.get("OLLAMA_HOST", "http://localhost:11434")
            self.ollama_model_primary: str = os.environ.get("OLLAMA_MODEL_PRIMARY", "llama3.1:8b")
            self.ollama_model_fallback: str = os.environ.get("OLLAMA_MODEL_FALLBACK", "llama3.2:1b")

            self.imap_host: str = os.environ.get("IMAP_HOST", "imap.gmail.com")
            self.imap_port: int = int(os.environ.get("IMAP_PORT", "993"))
            self.imap_username: Optional[str] = os.environ.get("IMAP_USERNAME")
            self.imap_password: Optional[str] = os.environ.get("IMAP_PASSWORD")
            self.smtp_host: str = os.environ.get("SMTP_HOST", "smtp.gmail.com")
            self.smtp_port: int = int(os.environ.get("SMTP_PORT", "587"))
            self.smtp_username: Optional[str] = os.environ.get("SMTP_USERNAME")
            self.smtp_password: Optional[str] = os.environ.get("SMTP_PASSWORD")
            self.email_sending_domain: str = os.environ.get("EMAIL_SENDING_DOMAIN", "sales@yourcompany.com")
            self.email_tracking_enabled: bool = os.environ.get("EMAIL_TRACKING_ENABLED", "true").lower() == "true"

            self.sla_high_urgency_hours: int = int(os.environ.get("SLA_HIGH_HOURS", "24"))
            self.sla_medium_urgency_hours: int = int(os.environ.get("SLA_MEDIUM_HOURS", "48"))
            self.sla_low_urgency_hours: int = int(os.environ.get("SLA_LOW_HOURS", "72"))
            self.queue_max_items_per_rep: int = int(os.environ.get("QUEUE_MAX_ITEMS", "50"))
            self.priority_score_decay_days: float = float(os.environ.get("PRIORITY_DECAY_DAYS", "7.0"))

            self.whisper_model_size: str = os.environ.get("WHISPER_MODEL_SIZE", "base")

            self.suppressions_list_path: Optional[str] = os.environ.get("SUPPRESSIONS_LIST_PATH")
            self.gdpr_compliance_enabled: bool = os.environ.get("GDPR_COMPLIANCE_ENABLED", "true").lower() == "true"
            self.ccpa_compliance_enabled: bool = os.environ.get("CCPA_COMPLIANCE_ENABLED", "true").lower() == "true"

            self.host: str = os.environ.get("HOST", "0.0.0.0")
            self.port: int = int(os.environ.get("PORT", "8000"))
            self.debug: bool = os.environ.get("DEBUG", "false").lower() == "true"

            self.pipeline_enabled: bool = os.environ.get("PIPELINE_ENABLED", "true").lower() == "true"

            self.prometheus_enabled: bool = os.environ.get("PROMETHEUS_ENABLED", "true").lower() == "true"
            self.metrics_port: int = int(os.environ.get("METRICS_PORT", "9090"))

            self.tracking_base_url: str = os.environ.get("TRACKING_BASE_URL", "http://localhost:8000/track")
            self.google_client_id: Optional[str] = os.environ.get("GOOGLE_CLIENT_ID")
            self.google_client_secret: Optional[str] = os.environ.get("GOOGLE_CLIENT_SECRET")
            self.google_redirect_uri: str = os.environ.get("GOOGLE_REDIRECT_URI", "http://localhost:8000/auth/google/callback")
            self.gmail_token_path: str = os.environ.get("GMAIL_TOKEN_PATH", "data/gmail_token.json")
            self.use_redis: bool = os.environ.get("USE_REDIS", "false").lower() == "true"
            self.redis_url: str = os.environ.get("REDIS_URL", "redis://localhost:6379")
            self.database_url: Optional[str] = os.environ.get("DATABASE_URL")
            self.api_keys: str = os.environ.get("API_KEYS", "")
            self.allowed_origins: str = os.environ.get("ALLOWED_ORIGINS", "")
            self.auth_required: bool = os.environ.get("AUTH_REQUIRED", "true").lower() == "true"
            self.api_base_url: str = os.environ.get("API_BASE_URL", "http://localhost:8000")
            self.inspector_enabled: bool = os.environ.get("INSPECTOR_ENABLED", "true").lower() == "true"
            self.enforce_2fa_admin: bool = os.environ.get("ENFORCE_2FA_ADMIN", "false").lower() == "true"
            self.enforce_2fa_rep: bool = os.environ.get("ENFORCE_2FA_REP", "false").lower() == "true"
            self.air_gapped: bool = os.environ.get("AIR_GAPPED", "false").lower() == "true"
            self.trusted_proxies: str = os.environ.get("TRUSTED_PROXIES", "")

            # 2FA Recovery
            self.recovery_link_ttl_hours: int = int(os.environ.get("RECOVERY_LINK_TTL_HOURS", "1"))
            self.recovery_max_links: int = int(os.environ.get("RECOVERY_MAX_LINKS", "3"))
            self.recovery_base_url: str = os.environ.get("RECOVERY_BASE_URL", "http://localhost:8000")


# Singleton
_settings_instance: Optional[Settings] = None


def get_settings() -> Settings:
    """Get the global settings instance."""
    global _settings_instance
    if _settings_instance is None:
        _settings_instance = Settings()
    return _settings_instance


def reload_settings() -> Settings:
    """Force reload settings (e.g., after env var changes)."""
    global _settings_instance
    _settings_instance = Settings()
    return _settings_instance
