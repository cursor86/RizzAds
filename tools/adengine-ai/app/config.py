import os
from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    database_url: str = "postgresql+asyncpg://adengine:adengine@localhost:5432/adengine"
    api_key: str
    auto_create_schema: bool = False

    meta_api_version: str = "v23.0"

    google_developer_token: str = ""
    google_client_id: str = ""
    google_client_secret: str = ""
    google_login_customer_id: str = ""

    # Organic publishing (Instagram Reels and Facebook Reels). Used by app/organic/.
    meta_app_id: str = ""
    meta_app_secret: str = ""
    organic_api_version: str = "v19.0"
    # Fernet key for encrypting client tokens at rest. Generate with cryptography.fernet.Fernet.generate_key().
    encryption_key: str = ""
    organic_poll_max_seconds: int = 900
    organic_max_attempts: int = 3
    token_refresh_days_before_expiry: int = 7
    token_expiry_margin_hours: int = 1

    anthropic_api_key: str = ""
    anthropic_model: str = "claude-sonnet-5-5"

    lookback_days: int = 7
    kill_cpa_multiple: float = 2.0
    kill_min_hook_impressions: int = 2000
    kill_min_hook_rate_pct: float = 15.0
    scale_step: float = 0.15
    scale_cooldown_hours: int = 72
    scale_min_conversion_days: int = 3
    scale_cpa_stability_cv: float = 0.25
    iteration_min_conversions: int = 20
    iteration_roas_multiple: float = 1.5
    iteration_cooldown_days: int = 7


@lru_cache
def get_settings() -> Settings:
    return Settings()


def resolve_secret(ref: str) -> str:
    """AdAccount stores the NAME of an environment variable holding its token, never the token."""
    value = os.environ.get(ref)
    if not value:
        raise RuntimeError(f"Secret reference '{ref}' is not set in the environment")
    return value
