from functools import lru_cache
from typing import Annotated

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict


class Settings(BaseSettings):
    """
    Everything this service needs from its environment, in one object.

    Read once at import and cached. Scattering ``os.environ`` through the code
    means a missing variable surfaces as an AttributeError on the request that
    happens to need it, hours after deploy — here a bad environment fails at
    boot, which is when someone is still watching.
    """

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # --- Runtime ----------------------------------------------------------
    environment: str = "development"
    debug: bool = False

    @property
    def is_production(self) -> bool:
        return self.environment.lower() in {"production", "prod"}

    # --- Database ---------------------------------------------------------
    database_url: str = "postgresql+asyncpg://als:als@localhost:5432/als"

    #: Transaction-mode poolers multiplex one server connection across clients,
    #: so a prepared statement cached by asyncpg can be replayed on a
    #: connection that never saw it. Disabling the cache is the price.
    database_use_pgbouncer: bool = False
    database_pool_size: int = 10
    database_max_overflow: int = 20
    database_echo: bool = False

    @field_validator("database_url")
    @classmethod
    def _must_be_async(cls, value: str) -> str:
        """
        Forces the asyncpg driver, rewriting the URL where that is unambiguous.

        A bare ``postgresql://`` URL loads the *sync* driver without complaint,
        and every query then blocks the event loop. That is close to impossible
        to spot from the outside — the service just gets mysteriously slow under
        load.

        Hosted Postgres hands out exactly that form (Render, Heroku and Supabase
        all do, some still with the legacy ``postgres://`` scheme), so rejecting
        it means every deploy depends on someone remembering to retype the
        connection string. Since a driverless URL states no driver preference,
        it is upgraded here rather than refused. A URL naming some *other*
        driver is a real disagreement and still fails.
        """
        for scheme in ("postgresql+asyncpg://",):
            if value.startswith(scheme):
                return value

        for scheme in ("postgresql://", "postgres://"):
            if value.startswith(scheme):
                return "postgresql+asyncpg://" + value[len(scheme) :]

        raise ValueError(
            "DATABASE_URL must use the asyncpg driver: postgresql+asyncpg://..."
        )

    # --- Auth -------------------------------------------------------------
    jwt_secret: str = "change-me-in-every-environment"
    jwt_algorithm: str = "HS256"
    jwt_access_ttl_minutes: int = 30
    jwt_refresh_ttl_days: int = 60

    # --- Supabase Storage -------------------------------------------------
    supabase_url: str = ""
    supabase_service_key: str = ""
    supabase_storage_signed_url_ttl: int = 3600

    @property
    def storage_configured(self) -> bool:
        return bool(self.supabase_url and self.supabase_service_key)

    # --- SMS --------------------------------------------------------------
    #: Celcom Africa. Absent in development, where codes are logged instead of
    #: sent — see app/services/sms.py.
    sms_api_key: str = ""
    sms_partner_id: str = ""
    #: The registered sender name shown on the handset.
    sms_sender_id: str = ""

    @property
    def sms_configured(self) -> bool:
        return bool(self.sms_api_key and self.sms_partner_id)

    # --- Google sign-in ---------------------------------------------------
    #: The OAuth client ids the mobile app uses. An ID token is only accepted
    #: if its audience is one of these — without the check, a token minted for
    #: any other Google app would authenticate here.
    google_client_ids: Annotated[list[str], NoDecode] = Field(default_factory=list)

    @field_validator("google_client_ids", mode="before")
    @classmethod
    def _split_client_ids(cls, value: object) -> object:
        if isinstance(value, str):
            return [item.strip() for item in value.split(",") if item.strip()]
        return value

    # --- OTP --------------------------------------------------------------
    otp_ttl_seconds: int = 600
    otp_max_attempts: int = 5
    #: Codes allowed per number per window. An unthrottled send endpoint is a
    #: bill someone else can run up.
    otp_max_sends_per_hour: int = 5

    # --- Payments ---------------------------------------------------------
    paystack_secret_key: str = ""
    paystack_webhook_secret: str = ""

    # --- Outbound ---------------------------------------------------------
    #: Every outbound call gets a deadline. A hung upstream must fail in
    #: seconds rather than hold a worker until the container is killed.
    http_timeout_seconds: float = 15.0

    # --- CORS -------------------------------------------------------------
    #: `NoDecode` matters: pydantic-settings tries to JSON-decode any complex
    #: type straight from the environment, *before* a `mode="before"` validator
    #: gets a look. Without it, a plain comma-separated CORS_ORIGINS blows up
    #: at import with a JSONDecodeError that names the wrong culprit.
    cors_origins: Annotated[list[str], NoDecode] = Field(default_factory=list)

    @field_validator("cors_origins", mode="before")
    @classmethod
    def _split_origins(cls, value: object) -> object:
        if isinstance(value, str):
            return [origin.strip() for origin in value.split(",") if origin.strip()]
        return value

    def assert_production_ready(self) -> None:
        """
        Called at startup. Refuses to serve production traffic with a default
        secret — the one misconfiguration that is silent, permanent and
        catastrophic.
        """
        if not self.is_production:
            return

        problems: list[str] = []
        if self.jwt_secret == "change-me-in-every-environment":
            problems.append("JWT_SECRET is still the default")
        if not self.storage_configured:
            problems.append("Supabase storage is not configured")
        if self.debug:
            problems.append("DEBUG is on")

        if problems:
            raise RuntimeError("Refusing to start: " + "; ".join(problems))


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()


settings = get_settings()
