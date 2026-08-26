"""
What the service refuses to start over, and what it merely warns about.

This file exists because the line between those two moved once already, in the
wrong direction. A check was added that refused to boot when the payment keys
were blank — which meant that setting ENVIRONMENT=production on a deploy whose
Kora keys had not been pasted in yet took the *whole product* down: no sign-in,
no notes, no timetable, to protect a checkout nobody could reach anyway.

The rule these tests hold:

  Refuse when serving would be **unsafe for everyone**.
  Degrade when a **feature** is unavailable.
"""

from app.core.config import Settings

REAL_SECRET = "a-real-secret-value-not-the-default"


def _production(**overrides) -> Settings:
    base = {
        "environment": "production",
        "debug": False,
        "jwt_secret": REAL_SECRET,
    }
    return Settings(**{**base, **overrides})


# --- Refuses ------------------------------------------------------------------


def test_the_default_signing_secret_stops_the_boot():
    """
    The one misconfiguration with no safe degraded mode.

    A known signing secret means anyone can mint a token for any account,
    including an admin one. There is nothing to serve safely, so nothing is
    served.
    """
    settings = _production(jwt_secret="change-me-in-every-environment")

    problems = settings.fatal_misconfigurations()
    assert problems
    assert "JWT_SECRET" in problems[0]


def test_debug_in_production_stops_the_boot():
    settings = _production(debug=True)
    assert settings.fatal_misconfigurations()


def test_development_is_never_refused():
    """
    Local work must not need a full production environment to run at all.
    """
    settings = Settings(
        environment="development",
        debug=True,
        jwt_secret="change-me-in-every-environment",
    )
    assert settings.fatal_misconfigurations() == []
    settings.assert_production_ready()


# --- Degrades -----------------------------------------------------------------


def test_production_starts_with_every_integration_missing():
    """
    The regression this file is named for.

    Blank storage, payment, SMS and Google settings must not stop the service.
    Each of those endpoints already reports itself unconfigured; the rest of
    the product is unaffected and has no business being taken down with them.
    """
    settings = _production(
        supabase_url="",
        supabase_service_key="",
        kora_secret_key="",
        public_base_url="",
        sms_api_key="",
        sms_partner_id="",
        google_client_ids=[],
    )

    assert settings.fatal_misconfigurations() == []
    settings.assert_production_ready()  # must not raise


def test_missing_integrations_are_named_in_words():
    """
    The warnings are the answer to a support question, so they have to say
    which variable and what stops working — not just that something is unset.
    """
    settings = _production(
        supabase_url="",
        supabase_service_key="",
        kora_secret_key="",
        sms_api_key="",
        sms_partner_id="",
        google_client_ids=[],
    )

    warnings = " | ".join(settings.unavailable_features())

    assert "SUPABASE_URL" in warnings and "/materials" in warnings
    assert "KORA_SECRET_KEY" in warnings and "/billing" in warnings
    assert "SMS_API_KEY" in warnings
    assert "GOOGLE_CLIENT_IDS" in warnings


def test_a_fully_configured_production_warns_about_nothing():
    settings = _production(
        supabase_url="https://project.supabase.co",
        supabase_service_key="service-key",
        kora_secret_key="sk_live_x",
        public_base_url="https://als.ardena.xyz",
        sms_api_key="k",
        sms_partner_id="p",
        google_client_ids=["client-id"],
        cors_origins=["https://admin.ardena.xyz"],
        deepseek_api_key="sk-live",
    )

    assert settings.unavailable_features() == []


def test_a_missing_ai_key_is_a_warning_not_a_refusal():
    """
    The tutor is a feature, not the product.

    Without a provider key /tutor/ask refuses and every other screen — notes,
    timetable, sync, billing — carries on exactly as before. Refusing to boot
    over it would take the whole thing down to protect one endpoint.
    """
    settings = _production(deepseek_api_key="")

    assert settings.fatal_misconfigurations() == []
    assert any("AI provider" in warning for warning in settings.unavailable_features())


def test_a_missing_public_base_url_is_only_flagged_once_payments_are_on():
    """
    Ordering that keeps the warning list honest.

    With no Kora key there is no checkout, so there are no webhooks to lose and
    saying so would be noise. With a key and no public origin, every charge
    goes out carrying no notification URL — the money moves and nothing here is
    ever told, which is worth a line of its own.
    """
    without_payments = _production(kora_secret_key="", public_base_url="")
    assert not any("PUBLIC_BASE_URL" in w for w in without_payments.unavailable_features())

    with_payments = _production(kora_secret_key="sk_live_x", public_base_url="")
    assert any("PUBLIC_BASE_URL" in w for w in with_payments.unavailable_features())


def test_empty_cors_is_flagged_in_production_only():
    """
    An empty CORS list is normal locally and a broken admin console in
    production, so it is only worth saying in one of those.
    """
    assert any(
        "CORS_ORIGINS" in warning
        for warning in _production(cors_origins=[]).unavailable_features()
    )

    development = Settings(environment="development", jwt_secret=REAL_SECRET, cors_origins=[])
    assert not any("CORS_ORIGINS" in w for w in development.unavailable_features())


# --- The webhook address ------------------------------------------------------


def test_the_webhook_url_is_built_from_the_public_origin():
    settings = _production(public_base_url="https://als.ardena.xyz")
    assert settings.webhook_url == "https://als.ardena.xyz/api/v1/billing/webhook"


def test_a_trailing_slash_does_not_double_up():
    """`https://host//api/v1/...` is a 404 that reads like a routing bug."""
    settings = _production(public_base_url="https://als.ardena.xyz/")
    assert settings.webhook_url == "https://als.ardena.xyz/api/v1/billing/webhook"


def test_no_public_origin_means_no_webhook_url():
    """
    Blank rather than a guess. A wrong notification URL is worse than none:
    Kora would post completed charges into the void and the absence would look
    like a provider outage.
    """
    assert _production(public_base_url="").webhook_url == ""
