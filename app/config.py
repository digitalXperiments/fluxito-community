# Bootstrap auto-generated secrets BEFORE Settings() validates env.
from app.bootstrap_secrets import bootstrap_secrets

bootstrap_secrets()

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from app._version import get_version as _get_version


class Settings(BaseSettings):
    # Hybrid model (per user request 2026-05-27):
    # .env / .env.local is used ONLY for the true bootstrap values:
    #   - APP_SECRET_KEY, TOKEN_ENCRYPTION_KEY
    #   - DATABASE_URL, REDIS_URL
    # All other settings (platform OAuth apps, email, dashboards, Sentry,
    # rate limits, etc.) live in the database and are managed via the web UI.
    #
    # .env.local is written by bootstrap_secrets() on first boot and takes
    # priority over .env (later files win in pydantic-settings).
    model_config = SettingsConfigDict(env_file=(".env", ".env.local"), extra="ignore")

    # Application
    APP_ENV: str = "development"
    APP_SECRET_KEY: str = Field(..., min_length=32)
    APP_BASE_URL: str = "http://localhost:8000"

    # URL the in-container headless browser uses to reach this app when
    # rendering dashboard PDFs. Defaults to the app's own uvicorn port so the
    # PDF renderer never has to leave the container (no nginx / public DNS).
    INTERNAL_BASE_URL: str = "http://127.0.0.1:8001"

    # Hard ceiling (seconds) for a single dashboard PDF render. Chromium nav +
    # async card hydration + chart paint must finish within this budget.
    PDF_RENDER_TIMEOUT_S: int = 60

    # Database
    DATABASE_URL: str = "postgresql+asyncpg://postgres:postgres@localhost:5432/fluxito"

    @field_validator("DATABASE_URL")
    @classmethod
    def fix_db_scheme(cls, v: str) -> str:
        """Fly Managed Postgres sets postgres:// — rewrite for asyncpg."""
        if v.startswith("postgres://"):
            v = v.replace("postgres://", "postgresql+asyncpg://", 1)
        elif v.startswith("postgresql://"):
            v = v.replace("postgresql://", "postgresql+asyncpg://", 1)
        return v

    # Redis
    REDIS_URL: str = "redis://localhost:6379/0"

    # Token encryption (Fernet)
    TOKEN_ENCRYPTION_KEY: str

    # Google OAuth app — last special-case env OAuth client because it powers
    # both login and Google-family connectors during bootstrap/sign-in.
    GOOGLE_CLIENT_ID: str = ""
    GOOGLE_CLIENT_SECRET: str = ""

    # Deprecated env fallbacks for runtime settings. Configure these in
    # Settings -> System for new installs; the fields remain here so existing
    # deployments keep booting until their DB settings are saved.

    # Adobe IMS (shared by Analytics + Launch)
    ADOBE_IMS_TOKEN_URL: str = "https://ims-na1.adobelogin.com/ims/token/v3"

    # Email (SMTP) — leave empty to log emails to console in dev
    SMTP_HOST: str = ""
    SMTP_PORT: int = 587
    SMTP_USERNAME: str = ""
    SMTP_PASSWORD: str = ""
    SMTP_FROM_EMAIL: str = "noreply@example.com"  # Change this in Settings → System after first admin login
    SMTP_FROM_NAME: str = "Fluxito"

    # CORS
    CORS_ALLOWED_ORIGINS: str = "https://claude.ai,https://chatgpt.com"

    # Sentry (optional — leave blank to disable)
    SENTRY_DSN: str = ""
    SENTRY_TRACES_SAMPLE_RATE: float = 0.1  # 10% of requests traced

    # Rate limits — soft API throttle. Defaults are sane; only set if you need to override.
    RATE_LIMIT_PER_MIN: int = 60
    RATE_LIMIT_PER_HOUR: int = 1000
    RATE_LIMIT_PER_DAY: int = 10000

    # Beta / instance operations — all runtime-overridable via the Admin panel.
    MAINTENANCE_MODE: bool = False  # When True, only super-admins can use the app.
    CHAT_ENABLED: bool = True  # Enable the in-app Chat and personal model settings.
    ANNOUNCEMENT_BANNER: str = ""  # Site-wide banner shown to signed-in users.
    AUTH_GOOGLE_ENABLED: bool = True  # Show "Continue with Google" on sign-in.
    AUTH_PASSWORD_ENABLED: bool = False  # Allow email + password sign-in.
    UPDATE_CHECKS_ENABLED: bool = True  # Check GitHub for newer releases; disable for air-gapped installs.
    GTM_CONTAINER_ID: str = ""  # Google Tag Manager container ID for site tracking (e.g. GTM-XXXXXXX).

    # MCP OAuth
    MCP_ALLOWED_REDIRECT_URIS: str = "https://claude.ai/api/mcp/auth_callback"
    MCP_SERVER_NAME: str = "fluxito"
    MCP_SERVER_VERSION: str = _get_version()  # evaluated once at import; override via APP_VERSION env

    # When True (default), the /oauth/authorize flow can detect a valid browser
    # (uid cookie) session and offer email/password sign-in + explicit consent.
    # When False, the original strict Google-identity-only behavior is used.
    MCP_OAUTH_ALLOW_BROWSER_SESSION: bool = True
    ENABLED_TOOLS: str = Field(
        default="all", description="Comma-separated list of tool domains to load (e.g., 'bq,gtm,ga4,ads')"
    )

    # Demo mode — set to the demo viewer's email to block MCP access for that
    # account while allowing full web UI browsing. Leave empty to disable.
    DEMO_VIEWER_EMAIL: str = ""

    @property
    def mcp_allowed_redirect_uris_list(self) -> list[str]:
        return [uri.strip() for uri in self.MCP_ALLOWED_REDIRECT_URIS.split(",")]


settings = Settings()  # type: ignore[call-arg]  # fields loaded from env vars
