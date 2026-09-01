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

    #: Two sources, in increasing priority: a local .env for development, and
    #: the operator-managed file on a server. Real environment variables still
    #: beat both, so systemd's EnvironmentFile stays authoritative for the
    #: service itself.
    #:
    #: The second path is what makes the ops scripts work. systemd hands the
    #: settings to the API and the worker, but `scripts/create_admin.py` run
    #: over SSH inherits nothing — so it fell back to the built-in default
    #: connection string and tried to reach a database on localhost, on a box
    #: whose Postgres is in another country. Reading the file directly means a
    #: script behaves the same way the service does, without ceremony at the
    #: call site. Missing files are ignored, so this is inert on a laptop.
    model_config = SettingsConfigDict(
        env_file=(".env", "/etc/als-backend/env"),
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

    # --- Admin console ----------------------------------------------------
    #: Shorter than a student's. A console session left open on a laptop is a
    #: different risk from an app on a phone that is already locked.
    admin_access_ttl_minutes: int = 60
    admin_refresh_ttl_days: int = 7

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

    # --- Push notifications -----------------------------------------------
    #
    # Expo's push service needs no credential to accept a send, which makes a
    # key a bad switch: there would be nothing to leave unset in development,
    # and every test run would fire real notifications at whatever tokens the
    # database happened to hold. So the switch is explicit. Off means the
    # reminder sweep still runs and still decides what to send — it writes the
    # notification to the log instead of the handset.
    push_enabled: bool = False
    #: Only needed once the Expo project turns on enhanced security. Sent as a
    #: bearer token when present, ignored when not.
    expo_access_token: str = ""
    #: How long Expo may keep trying. A class reminder is worthless an hour
    #: late, so this is deliberately short rather than the multi-day default.
    push_ttl_seconds: int = 1800

    #: Minutes between reminder sweeps. Also the granularity of a reminder: a
    #: 15-minute lead lands somewhere in the minute before, never earlier.
    reminder_sweep_seconds: int = 60

    @property
    def push_configured(self) -> bool:
        return self.push_enabled

    # --- The store review account -----------------------------------------
    #
    # Google Play and the App Store both want a working login in the review
    # notes, and this product has no password to give them: sign-in is Google
    # or a code texted to a phone the reviewer does not hold.
    #
    # So one number is declared here as a review account. Asking for a code on
    # it sends no SMS and writes no code — the code is the fixed one below, and
    # it is the only one that number ever accepts. Everything after that is an
    # ordinary account: real tokens, real data, the same endpoints.
    #
    # Blank means the account does not exist at all, which is the right default
    # everywhere except the environment that actually faces a reviewer. The
    # number must be one nobody can be issued in real life, or a student could
    # be handed a phone number that signs in without a code.
    review_phone: str = ""
    review_otp_code: str = ""

    @property
    def review_account_configured(self) -> bool:
        return bool(self.review_phone and self.review_otp_code)

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

    # --- Payments: M-Pesa, direct ------------------------------------------
    #
    # Daraja is Safaricom's own API and the default way a student pays, because
    # it is the cheapest: Kora and Paystack each take a percentage for standing
    # between us and the same M-Pesa transaction. On a KES 150 plan that spread
    # is most of the margin.
    #
    # With these unset the whole M-Pesa path falls back to Kora, quietly and by
    # design — `daraja.configured()` is checked before a provider is chosen, so
    # a deployment without credentials never puts a student through a request
    # that cannot work.
    daraja_consumer_key: str = ""
    daraja_consumer_secret: str = ""

    #: The paybill or till number the money lands in.
    daraja_shortcode: str = ""

    #: The Lipa na M-Pesa passkey. Half of the per-request password, with the
    #: shortcode and a timestamp — see `app/services/daraja.py`.
    daraja_passkey: str = ""

    #: `sandbox` or `production`. Anything but "production" is sandbox, so a
    #: typo cannot accidentally point a test deployment at real money.
    daraja_environment: str = "sandbox"

    #: `CustomerPayBillOnline` for a paybill, `CustomerBuyGoodsOnline` for a
    #: till. Sending the wrong one is rejected with an error naming a field
    #: rather than the mismatch, which is a slow afternoon.
    daraja_transaction_type: str = "CustomerPayBillOnline"

    #: The **till** number, on a Buy Goods account. Blank means "same as the
    #: shortcode", which is correct for a paybill.
    #:
    #: On Buy Goods the pair is: `daraja_shortcode` is the *store* (head office)
    #: number — the one the passkey is issued against — and this is the till the
    #: money lands in. They are different numbers and swapping them fails.
    daraja_party_b: str = ""

    @property
    def daraja_callback_url(self) -> str:
        """
        Where Safaricom posts the result of a prompt.

        Derived from the public origin for the same reason Kora's is: behind
        nginx the app sees `127.0.0.1:8000`, so a URL cannot be built from the
        inbound request, and handing Safaricom that address means a payment
        nobody is ever told about.
        """
        if self.daraja_callback_override:
            return self.daraja_callback_override
        if not self.public_base_url:
            return ""
        return f"{self.public_base_url.rstrip('/')}/api/v1/billing/mpesa/callback"

    #: Set only when the callback has to go somewhere other than this service —
    #: a tunnel while developing against the sandbox, most likely.
    daraja_callback_override: str = ""

    @property
    def mpesa_configured(self) -> bool:
        return bool(
            self.daraja_consumer_key
            and self.daraja_consumer_secret
            and self.daraja_shortcode
            and self.daraja_passkey
        )

    # --- Payments: the fallback --------------------------------------------
    #
    # Kora (korahq.com). Two things differ from the Paystack integration this
    # replaced, and both are silent when wrong: Kora charges in the **major**
    # unit (350 means KES 350, not 3.50), and it signs webhooks over only the
    # `data` object with SHA-256. See app/services/kora.py.
    kora_secret_key: str = ""
    #: Only used by a browser checkout widget, which this product does not have.
    #: Kept so the pair can be set together and nobody wonders where it went.
    kora_public_key: str = ""

    #: Kora signs webhooks with the secret key itself — there is no separate
    #: webhook secret to copy from the dashboard. This exists only to pin a
    #: different value if that ever changes; blank means "use the secret key".
    kora_webhook_secret: str = ""

    #: Where Kora sends the browser once a payment finishes.
    #:
    #: Blank means "use whatever the Kora dashboard is set to", which is the
    #: safe default: the app does not depend on this redirect, it verifies the
    #: reference it was given when the browser closes. Set it to the app's deep
    #: link (`als://billing`) to have the payment page bounce straight back into
    #: the app instead.
    kora_callback_url: str = ""

    # --- Payments: cards (Paystack) ----------------------------------------
    #
    # Cards are the one thing neither Daraja nor Kora covers here.
    #
    # This deployment shares a Paystack business with another product, and two
    # dashboard settings therefore belong to whoever set it up first:
    #
    #   · The callback URL is bypassed per transaction — `callback_url` on
    #     `transaction/initialize` overrides the dashboard for that transaction
    #     only, so the other app's setting is untouched.
    #   · The webhook URL **cannot** be bypassed. Paystack posts every event on
    #     the account to the one configured URL, which is the other app's, so
    #     this service never receives one and does not rely on one. Card
    #     payments settle by asking Paystack — on return, and again from the
    #     worker's sweep. See app/services/paystack.py.
    paystack_secret_key: str = ""
    #: Only used by a browser widget, which this product does not have. Kept so
    #: the pair can be set together and nobody wonders where it went.
    paystack_public_key: str = ""

    @property
    def paystack_callback_url(self) -> str:
        """
        Where the student comes back to after paying by card.

        Sent per transaction, which is what makes a borrowed account workable.
        Defaults to this service's own return page; set
        `PAYSTACK_CALLBACK_OVERRIDE` to send them into the app by deep link
        instead.
        """
        if self.paystack_callback_override:
            return self.paystack_callback_override
        if not self.public_base_url:
            return ""
        return f"{self.public_base_url.rstrip('/')}/api/v1/billing/card/return"

    paystack_callback_override: str = ""

    @property
    def cards_configured(self) -> bool:
        return bool(self.paystack_secret_key)

    #: Domain for the stand-in address used when an account has no email.
    #:
    #: Kora requires a customer email on every charge and phone sign-in does not
    #: collect one. The address is per-account and never receives mail — it
    #: exists so a charge can be opened at all, and `metadata.user_id` is what
    #: actually ties the payment to a student.
    receipt_email_domain: str = "als.ardena.xyz"

    # --- App releases -----------------------------------------------------
    #
    # Where the update modal sends someone who taps Update. Config rather than
    # a column because these two URLs never change once the app is listed, and
    # a per-release field nobody fills in is a per-release field somebody will
    # eventually get wrong. A release row can still override them.
    ios_store_url: str = ""
    android_store_url: str = ""

    @property
    def payments_configured(self) -> bool:
        return bool(self.kora_secret_key)

    # --- This service's own address ---------------------------------------
    #: The public origin, no trailing slash.
    #:
    #: Needed because a webhook URL cannot be derived from an inbound request:
    #: behind nginx the app sees `127.0.0.1:8000`, and handing Kora that would
    #: point every payment notification at the loopback interface. Setting it
    #: per environment is also what stops a staging deploy from registering
    #: itself for production's webhooks.
    public_base_url: str = ""

    @property
    def webhook_url(self) -> str:
        """Where Kora should post a completed charge. Blank if unknown."""
        if not self.public_base_url:
            return ""
        return f"{self.public_base_url.rstrip('/')}/api/v1/billing/webhook"

    # --- The tutor --------------------------------------------------------
    #
    # One key per provider. A provider with no key is *listed* by /tutor/models
    # and reported unavailable, rather than hidden — the app shows the full
    # line-up with the ones you cannot pick yet greyed out, and adding a key is
    # the only thing that turns one on.
    deepseek_api_key: str = ""
    openai_api_key: str = ""
    anthropic_api_key: str = ""
    google_api_key: str = ""

    #: What a student gets when they express no preference.
    ai_default_model: str = "deepseek-chat"

    # --- Reading photographs of pages -------------------------------------
    #
    # Chosen independently of `ai_default_model`, because the tutor's default is
    # DeepSeek and DeepSeek cannot see. OCR needs a model with vision whatever
    # the tutor is set to.
    #
    # Gemini by default: on a per-image basis it is the cheapest thing that
    # reads handwriting well, and the free tier covers a small cohort outright.

    #: Any endpoint speaking the OpenAI `/chat/completions` shape. Google,
    #: OpenRouter, Groq, Together and OpenAI itself all do, which is why this is
    #: a URL rather than a provider name — switching is an environment variable,
    #: not a new adapter.
    ocr_base_url: str = "https://generativelanguage.googleapis.com/v1beta/openai"
    ocr_model: str = "gemini-2.5-flash"

    #: Blank means "use `GOOGLE_API_KEY`" — see `ocr_key`.
    ocr_api_key: str = ""

    @property
    def ocr_key(self) -> str:
        """
        What OCR authenticates with.

        `OCR_API_KEY` wins, so a deployment can point scans at one provider and
        the tutor at another. It falls back to `GOOGLE_API_KEY` because that is
        the ordinary case — the default base URL is Google's, and asking someone
        to paste one key into two variables is a way to have them disagree
        later.

        Point `OCR_BASE_URL` at anything else and `OCR_API_KEY` becomes
        required: there is deliberately no guessing from the hostname.
        """
        return self.ocr_api_key or self.google_api_key

    #: Long enough for a full explanation, short enough that a runaway
    #: generation cannot bill for a novel.
    ai_max_output_tokens: int = 900

    #: Low, not zero. Coursework answers should be steady rather than
    #: inventive, but zero makes a model repeat itself almost word for word
    #: when a student rephrases the same question.
    ai_temperature: float = 0.3

    #: Separate from HTTP_TIMEOUT_SECONDS, which is 15s and right for Kora and
    #: Supabase. A model streaming 900 tokens routinely takes longer than that,
    #: and killing it at 15s would look like the tutor failing at random.
    ai_timeout_seconds: float = 120.0

    #: How many passages go into the prompt. Beyond about six, the useful ones
    #: get lost among the rest and the answer starts drifting.
    ai_retrieval_top_k: int = 6

    #: Below this, the student's own material is treated as not containing the
    #: answer, and the tutor says so before answering from general knowledge.
    #: The whole "I could not find this in your notes" behaviour turns on this
    #: number, so it is here rather than buried in the retrieval code.
    ai_retrieval_min_score: float = 0.04

    @property
    def tutor_configured(self) -> bool:
        return bool(
            self.deepseek_api_key
            or self.openai_api_key
            or self.anthropic_api_key
            or self.google_api_key
        )

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

    # --- Startup checks ---------------------------------------------------
    #
    # Two lists, and the line between them is the whole point.
    #
    # A *fatal* misconfiguration makes serving unsafe for everyone: a default
    # signing secret means anyone can mint a token for any account. There is no
    # degraded mode for that, so the process refuses to start and someone
    # notices while they are still watching the deploy.
    #
    # A *missing integration* makes one feature unavailable. Storage keys,
    # payment keys, an SMS provider — without them `/materials/*`, `/billing/*`
    # and real OTP delivery each report themselves unconfigured, and everything
    # else works exactly as it should. Refusing to boot over those would take
    # the whole product down to protect a part of it, which is the wrong trade
    # every time: a student cannot read their notes because nobody has pasted a
    # Kora key yet.

    def fatal_misconfigurations(self) -> list[str]:
        """Reasons this process must not serve traffic at all."""
        if not self.is_production:
            return []

        problems: list[str] = []
        if self.jwt_secret == "change-me-in-every-environment":
            problems.append(
                "JWT_SECRET is still the default — every token in the system "
                "would be forgeable"
            )
        if self.debug:
            problems.append("DEBUG is on, which leaks internals in error responses")

        return problems

    def unavailable_features(self) -> list[str]:
        """
        Features that will report themselves unconfigured, in plain words.

        Logged at startup and surfaced on the admin console's Operations page,
        so "why can nobody upload a PDF" has an answer that does not require
        reading the environment by hand.
        """
        missing: list[str] = []

        if not self.storage_configured:
            missing.append(
                "SUPABASE_URL / SUPABASE_SERVICE_KEY are not set — uploads and "
                "downloads under /materials will be refused"
            )
        if not self.payments_configured:
            missing.append(
                "KORA_SECRET_KEY is not set — /billing/checkout and "
                "/billing/verify will report that payments are unavailable"
            )
        elif not self.public_base_url:
            # Only worth saying once payments are on: without a public origin
            # Kora is given no notification_url, so a charge succeeds and
            # nothing here is ever told about it.
            missing.append(
                "PUBLIC_BASE_URL is not set — Kora charges will carry no webhook "
                "address, so payments will need reconciling by hand"
            )
        if not self.sms_configured:
            missing.append(
                "SMS_API_KEY / SMS_PARTNER_ID are not set — sign-in codes are "
                "written to the log instead of sent"
            )
        if not self.push_configured:
            missing.append(
                "PUSH_ENABLED is off — deadline and class reminders are decided "
                "and logged, but no notification reaches a handset"
            )
        if not self.google_client_ids:
            missing.append("GOOGLE_CLIENT_IDS is not set — /auth/google will refuse")
        if not self.tutor_configured:
            missing.append(
                "no AI provider key is set (DEEPSEEK_API_KEY and friends) — "
                "/tutor/ask will refuse and the app's tutor will be unavailable"
            )
        if self.is_production and not self.cors_origins:
            missing.append(
                "CORS_ORIGINS is empty — the admin console will be blocked by the "
                "browser before its requests reach this service"
            )

        return missing

    def assert_production_ready(self) -> None:
        """
        Called at startup. Raises only on the things that make serving unsafe.

        Everything else is a warning: see `unavailable_features`.
        """
        problems = self.fatal_misconfigurations()
        if problems:
            raise RuntimeError("Refusing to start: " + "; ".join(problems))


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()


settings = get_settings()
