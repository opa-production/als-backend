import time

from fastapi import APIRouter
from sqlalchemy import text

from app.api.deps import DbSession
from app.core.config import settings
from app.models.account import Device, User
from app.models.auth import OtpCode, RefreshToken
from app.models.billing import Payment, PlanGroup, Subscription
from app.models.knowledge import Material, MaterialChunk
from app.models.trial import TrialGrant
from app.models.tutor import Message
from app.schemas.admin import OpsHealthOut
from app.services import analytics

router = APIRouter()


@router.get("/health", response_model=OpsHealthOut, summary="Is anything wrong")
async def health(session: DbSession) -> OpsHealthOut:
    """
    The readiness picture, for a person rather than a load balancer.

    ``GET /health`` deliberately does not touch the database — a liveness probe
    that queries Postgres restarts every container the moment the database
    hiccups. This one does query it, because the question here is different:
    not "should this container be killed" but "why does the console look
    empty".

    The integration flags matter more than they look. A dashboard showing
    revenue while ``paystack`` is false is showing history from another
    environment, and that is a mistake worth one row on a page rather than an
    afternoon.
    """
    started = time.perf_counter()
    database_ok = True
    try:
        await session.execute(text("SELECT 1"))
    except Exception:
        database_ok = False
    latency_ms = round((time.perf_counter() - started) * 1000, 2)

    integrations = {
        "paystack": settings.payments_configured,
        "supabase_storage": settings.storage_configured,
        "sms": settings.sms_configured,
        "google_sign_in": bool(settings.google_client_ids),
    }

    counts = {
        "users": await analytics.count_rows(session, User, User.deleted_at.is_(None)),
        "devices": await analytics.count_rows(session, Device),
        "subscriptions": await analytics.count_rows(session, Subscription),
        "plan_groups": await analytics.count_rows(session, PlanGroup),
        "payments": await analytics.count_rows(session, Payment),
        "materials": await analytics.count_rows(
            session, Material, Material.deleted_at.is_(None)
        ),
        "material_chunks": await analytics.count_rows(session, MaterialChunk),
        "messages": await analytics.count_rows(session, Message),
        "trial_grants": await analytics.count_rows(session, TrialGrant),
        "live_refresh_tokens": await analytics.count_rows(
            session, RefreshToken, RefreshToken.revoked_at.is_(None)
        ),
        "otp_codes": await analytics.count_rows(session, OtpCode),
    }

    warnings: list[str] = []
    if not database_ok:
        warnings.append("The database did not answer.")
    for name, configured in integrations.items():
        if not configured:
            warnings.append(f"{name} has no credentials in this environment.")
    if settings.debug and settings.is_production:
        warnings.append("DEBUG is on in production.")

    content = await analytics.content_stats(session)
    if content.extraction_stalled:
        warnings.append(
            f"{content.extraction_stalled} materials have been waiting over an "
            "hour for extraction — the worker may be down."
        )

    return OpsHealthOut(
        environment=settings.environment,
        database_ok=database_ok,
        database_latency_ms=latency_ms,
        integrations=integrations,
        counts=counts,
        warnings=warnings,
    )


@router.get("/plans", summary="The plan catalogue, as the server sees it")
async def plans() -> list[dict]:
    """
    What the server believes each plan includes.

    ``src/theme/plans.js`` on the device holds a copy of these numbers, and the
    two are supposed to be identical. When a student insists their plan allows
    something the API refuses, this endpoint is the authoritative side of that
    argument — and the fastest way to spot that the app shipped with a stale
    copy.
    """
    from dataclasses import asdict

    from app.services.plans import PLANS

    return [
        {
            "id": plan.id.value,
            "name": plan.name,
            "price_ksh": plan.price_ksh,
            "price_per_seat_ksh": plan.price_per_seat_ksh,
            "duration_days": plan.duration_days,
            "seats": plan.seats,
            "limits": asdict(plan.limits),
        }
        for plan in PLANS.values()
    ]
