import uuid
from datetime import date, datetime

from fastapi import APIRouter, status
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import func, select

from app.api.deps import CurrentUser, DbSession, HttpClient
from app.core.clock import now as utc_now
from app.core.errors import NotFound
from app.models.account import Device
from app.models.notification import NotificationLog
from app.models.settings import UserSettings
from app.services import notifications as notification_service
from app.services import referrals
from app.services import streak as streak_service
from app.services.plans import UNLIMITED, plan_for
from app.services.quota import (
    current_usage,
    get_entitlement,
    quiz_metric,
    resets_on,
    user_zone,
)

router = APIRouter()


# --- Preferences --------------------------------------------------------------


class SettingsOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    deadline_reminders: bool
    class_reminders: bool
    reminder_lead_minutes: int
    quiet_hours_start: str
    quiet_hours_end: str
    timezone: str
    biometric_lock: bool
    biometric_kind: str


class SettingsUpdate(BaseModel):
    """Every field optional; unset fields are left alone."""

    deadline_reminders: bool | None = None
    class_reminders: bool | None = None
    reminder_lead_minutes: int | None = Field(default=None, ge=0, le=1440)
    quiet_hours_start: str | None = Field(default=None, pattern=r"^\d{2}:\d{2}$")
    quiet_hours_end: str | None = Field(default=None, pattern=r"^\d{2}:\d{2}$")
    timezone: str | None = Field(default=None, max_length=64)
    biometric_lock: bool | None = None
    biometric_kind: str | None = Field(default=None, max_length=16)


async def _settings(session: DbSession, user_id: uuid.UUID) -> UserSettings:
    row = await session.scalar(
        select(UserSettings).where(UserSettings.user_id == user_id)
    )
    if row is None:
        # Created on first read, so a student who never opens Settings still
        # has the defaults a reminder job can read.
        row = UserSettings(user_id=user_id)
        session.add(row)
        await session.flush()
    return row


@router.get("/settings", response_model=SettingsOut, summary="Your preferences")
async def read_settings(user: CurrentUser, session: DbSession) -> SettingsOut:
    """
    Notification and security preferences.

    `timezone` is the load-bearing one: without it the server cannot know when
    22:00 is for this person, and every reminder lands at the wrong hour for
    anyone who travels.
    """
    return SettingsOut.model_validate(await _settings(session, user.id))


@router.patch("/settings", response_model=SettingsOut, summary="Update preferences")
async def update_settings(
    payload: SettingsUpdate, user: CurrentUser, session: DbSession
) -> SettingsOut:
    """
    Changes only the fields present.

    `biometric_lock` is recorded here but enforced on the device — it guards
    the *view*, not this data. Everything is in the database either way, and
    saying otherwise would be a security claim this cannot back.
    """
    row = await _settings(session, user.id)

    for field_name, value in payload.model_dump(exclude_unset=True).items():
        setattr(row, field_name, value)

    await session.flush()
    return SettingsOut.model_validate(row)


# --- Devices ------------------------------------------------------------------


class DeviceIn(BaseModel):
    #: Minted by the client, so a reinstall updates one row instead of adding
    #: a new one on every launch.
    id: uuid.UUID
    platform: str = Field(default="", max_length=16)
    app_version: str = Field(default="", max_length=32)
    push_token: str | None = Field(default=None, max_length=256)


class DeviceOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    platform: str
    app_version: str
    has_push: bool = False


@router.put("/devices", response_model=DeviceOut, summary="Register this device")
async def register_device(
    payload: DeviceIn, user: CurrentUser, session: DbSession
) -> DeviceOut:
    """
    Records the installation and its push token.

    A PUT, not a POST: the device names itself, so calling it on every launch
    is an update rather than a pile of rows. The push token is stored here
    because it changes on its own schedule — reinstalls, restores, OS upgrades
    — and a stale one is a notification that silently goes nowhere.
    """
    device = await session.get(Device, payload.id)

    if device is None:
        device = Device(id=payload.id, user_id=user.id)
        session.add(device)

    device.user_id = user.id
    device.platform = payload.platform or device.platform
    device.app_version = payload.app_version or device.app_version
    if payload.push_token is not None:
        device.push_token = payload.push_token

    await session.flush()

    return DeviceOut(
        id=device.id,
        platform=device.platform,
        app_version=device.app_version,
        has_push=bool(device.push_token),
    )


@router.delete(
    "/devices/{device_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Stop notifications on a device",
)
async def forget_device(
    device_id: uuid.UUID, user: CurrentUser, session: DbSession
) -> None:
    """
    Clears the push token without deleting the device.

    The row still explains which installation a refresh token belongs to, so
    removing it would lose the ability to sign out one phone.
    """
    device = await session.get(Device, device_id)
    if device is None or device.user_id != user.id:
        raise NotFound("That device is not on your account.")

    device.push_token = None
    await session.flush()


# --- Notifications ------------------------------------------------------------


class TestPushOut(BaseModel):
    #: How many devices actually took it. Zero with `has_devices` true is the
    #: interesting case: a token that is registered but no longer live.
    delivered: int
    has_devices: bool


@router.post(
    "/push/test", response_model=TestPushOut, summary="Send yourself a notification"
)
async def test_push(
    user: CurrentUser, session: DbSession, client: HttpClient
) -> TestPushOut:
    """
    Sends a notification to every device on this account, immediately.

    "Are notifications working" is otherwise unanswerable without waiting for a
    real deadline — permission, the token, the Expo project and its credentials
    all fail the same silent way. Quiet hours are ignored: this was asked for by
    the person holding the phone.
    """
    devices = await session.scalar(
        select(func.count())
        .select_from(Device)
        .where(Device.user_id == user.id, Device.push_token.is_not(None))
    )

    delivered = await notification_service.send_test(
        session, user_id=user.id, client=client
    )

    return TestPushOut(delivered=delivered, has_devices=bool(devices))


class NotificationOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    kind: str
    title: str
    body: str
    status: str
    scheduled_for: datetime | None
    sent_at: datetime | None


@router.get(
    "/notifications",
    response_model=list[NotificationOut],
    summary="Reminders recently sent to you",
)
async def read_notifications(
    user: CurrentUser, session: DbSession, limit: int = 50
) -> list[NotificationOut]:
    """
    What the server has sent, newest first.

    Push is fire-and-forget — a notification that arrives while the phone is off
    is simply gone — so this is the app's in-app list, and the only way a
    student can see a reminder they missed.
    """
    rows = await session.scalars(
        select(NotificationLog)
        .where(NotificationLog.user_id == user.id)
        .order_by(NotificationLog.created_at.desc())
        .limit(max(1, min(limit, 100)))
    )
    return [NotificationOut.model_validate(row) for row in rows]


# --- Streak -------------------------------------------------------------------


class StreakOut(BaseModel):
    current: int
    longest: int
    last_day: date | None
    this_week: list[date]
    total_days: int


class StreakIn(BaseModel):
    #: The student's *local* day. Deriving it from a UTC timestamp breaks the
    #: streak of anyone revising after 3am, which is exactly this audience.
    day: date | None = None


@router.get("/streak", response_model=StreakOut, summary="Your streak")
async def read_streak(
    user: CurrentUser, session: DbSession, today: date | None = None
) -> StreakOut:
    """
    Days in a row, derived from the days themselves.

    Not having revised *yet* today does not break it — only a gap before
    yesterday does. Anything else would punish someone for opening the app at
    nine in the morning.
    """
    result = await streak_service.compute(
        session, user_id=user.id, today=today or utc_now().date()
    )
    return StreakOut(**result.__dict__)


@router.post("/streak", response_model=StreakOut, summary="Record today")
async def record_streak(
    payload: StreakIn, user: CurrentUser, session: DbSession
) -> StreakOut:
    """
    Marks a day as studied.

    Idempotent, so the app can call it on every question without counting
    first: three questions in one evening is one day of studying.
    """
    day = payload.day or utc_now().date()
    await streak_service.record_day(session, user_id=user.id, day=day)

    result = await streak_service.compute(session, user_id=user.id, today=day)
    return StreakOut(**result.__dict__)


# --- Referrals ----------------------------------------------------------------


class ReferralOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    #: Minted on the first read of this endpoint, then stable forever.
    code: str
    #: Signed up with the code. Not the same as earning anything.
    joined: int
    #: …and paid, which is what a reward is actually for.
    paid: int
    days_earned: int
    #: Earned but not yet on a plan — either inside the seven-day hold, or
    #: waiting for the student to subscribe.
    days_banked: int
    #: True when those banked days are waiting on a subscription rather than on
    #: the hold, so the app can say what they are waiting for.
    banked_pending_subscription: bool
    #: What a friend gets for using the code, so the share message and the
    #: server cannot disagree about what was promised.
    friend_days: int


@router.get(
    "/referrals", response_model=ReferralOut, summary="Your referral code"
)
async def read_referrals(user: CurrentUser, session: DbSession) -> ReferralOut:
    """
    The code, and what it has earned.

    Reading this is what mints the code — most accounts never open the screen,
    and a unique column filled in for all of them is a backfill bought for
    nothing.
    """
    result = await referrals.summary(session, user=user)
    return ReferralOut(
        code=result.code,
        joined=result.joined,
        paid=result.paid,
        days_earned=result.days_earned,
        days_banked=result.days_banked,
        banked_pending_subscription=result.banked_pending_subscription,
        friend_days=referrals.FRIEND_DAYS,
    )


# --- Usage --------------------------------------------------------------------


class MeterOut(BaseModel):
    used: int
    limit: int
    #: True where the plan sets no ceiling, so the client draws a full bar
    #: rather than dividing by a sentinel.
    unlimited: bool = False
    #: The day this meter refills, in the student's own timezone. ``None`` on a
    #: lifetime ceiling, which is not a reset and must not be drawn as a
    #: countdown to one.
    resets_at: date | None = None


class UsageOut(BaseModel):
    tier: str
    plan_name: str
    ai_queries_this_month: MeterOut
    #: Only meaningful where the plan sets a lifetime ceiling — Free. Elsewhere
    #: it reports as unlimited, and the app should not draw a bar for it.
    ai_queries_total: MeterOut
    quizzes: MeterOut
    quiz_interval: str
    course_units: MeterOut
    ocr_pages_this_month: MeterOut


@router.get("/usage", response_model=UsageOut, summary="What you have used")
async def read_usage(user: CurrentUser, session: DbSession) -> UsageOut:
    """
    Every meter on the Usage screen, in one call.

    Served from the same config the limits are enforced from, so the bars a
    student sees cannot disagree with the refusal they get.
    """
    from app.models.course import Unit

    entitlement = await get_entitlement(session, user.id)
    limits = entitlement.limits
    plan = plan_for(entitlement.tier)

    # Resolved once for every meter below: they all roll over on the same
    # clock, and looking it up per bar would be four reads for one answer.
    zone = await user_zone(session, user.id)

    def meter(used: int, limit: int, metric: str) -> MeterOut:
        return MeterOut(
            used=used,
            limit=limit,
            unlimited=limit == UNLIMITED,
            resets_at=resets_on(metric, zone),
        )

    metric = quiz_metric(limits)

    units_used = (
        await session.scalar(
            select(func.count())
            .select_from(Unit)
            .where(Unit.user_id == user.id, Unit.deleted_at.is_(None))
        )
    ) or 0

    return UsageOut(
        tier=entitlement.tier.value,
        plan_name=plan.name,
        ai_queries_this_month=meter(
            await current_usage(session, user.id, "ai_queries", zone),
            limits.monthly_ai_queries,
            "ai_queries",
        ),
        ai_queries_total=meter(
            await current_usage(session, user.id, "ai_queries_lifetime", zone),
            limits.lifetime_ai_queries,
            "ai_queries_lifetime",
        ),
        quizzes=meter(
            await current_usage(session, user.id, metric, zone),
            limits.quiz_count,
            metric,
        ),
        quiz_interval=limits.quiz_interval,
        # Units are a standing cap, not a meter that refills, so nothing here
        # rolls over and `resets_at` is deliberately absent.
        course_units=meter(units_used, limits.max_course_units, "pdf_pages"),
        ocr_pages_this_month=meter(
            await current_usage(session, user.id, "ocr_pages", zone),
            limits.monthly_ocr_page_limit if limits.allow_ocr_scans else 0,
            "ocr_pages",
        ),
    )
