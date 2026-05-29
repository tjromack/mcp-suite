"""Application configuration loaded from environment variables via pydantic-settings."""

from __future__ import annotations

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Singleton settings object. Import via `from core.config import settings`."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    database_url: str = "postgresql://ctmcp:ctmcp_password@localhost:5432/clinical_trials"

    # Anthropic — Phase 2 summarize_eligibility tool.
    anthropic_api_key: str = ""
    # Voyage AI — embeddings (Anthropic's recommended embedding partner).
    voyage_api_key: str = ""

    embedding_model: str = "voyage-3"
    summary_model: str = "claude-haiku-4-5-20251001"

    search_top_k: int = 10
    default_ingest_query: str = "cancer"
    default_ingest_max: int = 500

    # ClinicalTrials.gov sits behind Akamai Bot Manager, which 403s standard
    # HTTP clients on TLS/HTTP fingerprint. curl_cffi impersonates a real
    # browser to get through. "chrome" tracks a recent Chrome; pin a specific
    # version (e.g. "chrome124") if Akamai rules change.
    ctgov_impersonate: str = "chrome"
    # TLS verification for ClinicalTrials.gov calls. Keep True normally. Set
    # False ONLY on networks behind a TLS-intercepting proxy (corporate
    # firewall / AV SSL scanning) where libcurl can't build the cert chain —
    # or better, point ctgov_ca_bundle at your proxy's root CA PEM.
    ctgov_ssl_verify: bool = True
    ctgov_ca_bundle: str | None = None
    # In-memory TTL (seconds) for get_trial_details live fetches. Trial records
    # change rarely; this avoids re-hitting the Akamai-fronted API for repeat
    # lookups of the same NCT ID within a session. 0 disables caching.
    ctgov_cache_ttl_seconds: int = 3600


settings = Settings()
