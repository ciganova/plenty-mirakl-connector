"""
Central configuration via Pydantic Settings.

Single-tenant legacy fields are KEPT (mirakl_*, plenty_*) so the existing
single-tenant deployment keeps working — they seed the `default` tenant on
first migration. Multi-tenant runtime reads connections from DB.
"""
from functools import lru_cache
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env", env_file_encoding="utf-8", extra="ignore"
    )

    # --- Mirakl (legacy single-tenant; seeds default tenant on migration) ---
    mirakl_base_url: str = "https://your-shop.mirakl.net"
    mirakl_api_key: str = ""
    mirakl_shop_id: int = 0

    # --- PlentyONE (legacy single-tenant) ---
    plenty_base_url: str = "https://your-shop.plentymarkets.com"
    plenty_username: str = ""
    plenty_password: str = ""
    plenty_referrer_id: int = 1
    plenty_warehouse_id: int = 1
    plenty_plenty_id: int = 0

    # --- Database ---
    database_url: str = "postgresql+asyncpg://connector:secret@postgres:5432/connector"

    # --- Redis / Celery ---
    redis_url: str = "redis://redis:6379/0"

    # --- App Behavior ---
    dry_run: bool = False
    log_level: str = "INFO"
    order_poll_interval: int = 60
    tracking_poll_interval: int = 300

    # --- Traefik ---
    traefik_domain: str = "connector.domain.de"

    # --- SaaS: secrets ---
    # Fernet key for at-rest encryption of per-tenant Mirakl/Plenty creds.
    # Generate with: python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
    fernet_key: str = ""  # REQUIRED via env. Generate: python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"

    # Long random token for /panel/admin and /api/admin/* and saas_admin.py
    admin_api_key: str = "admin-changeme-set-via-env"

    # Cookie-signing secret for the panel session (set in env in prod)
    session_secret: str = "session-changeme-set-via-env"

    # --- SaaS: Stripe ---
    stripe_secret_key: str = ""           # sk_test_... or sk_live_...
    stripe_webhook_secret: str = ""       # whsec_...
    stripe_price_default: str = ""        # price_... for the €29 plan
    stripe_price_overage: str = ""        # price_... for €0.15/order metered (Phase 2)

    # --- SaaS: alerting ---
    ops_email: str = "romic@vagabond-consulting.com"
    billionmail_api_url: str = ""         # http://billionmail.servicebox/...
    billionmail_token: str = ""

    # --- SaaS: panel ---
    panel_refresh_seconds: int = 5


@lru_cache
def get_settings() -> Settings:
    return Settings()
