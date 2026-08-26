"""
Request and response shapes for the console.

Separate from ``app/schemas/account.py`` because the audiences are different in
a way that matters: the mobile app is shown what a student may see about
themselves, and the console is shown everything about everyone. Sharing a
response model between the two is how a phone number ends up in a payload that
was only ever reviewed as an admin screen.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field

#: Not ``EmailStr``. That pulls in ``email-validator`` to police a field whose
#: only job is to match a row that already exists — a dependency for a check
#: the database performs anyway. This rejects what is obviously not an address
#: and leaves the rest to the unique index.
EMAIL_PATTERN = r"^[^@\s]+@[^@\s]+\.[^@\s]+$"

class FromDataclass(BaseModel):
    """
    A response model fed directly from the dataclasses in
    ``app/services/analytics.py``.

    ``from_attributes`` is what lets a handler return the service's own result
    without restating twenty fields at the boundary. The alternative — a dict
    comprehension per endpoint — is twenty chances to typo a field name into a
    silent zero on a revenue dashboard.
    """

    model_config = ConfigDict(from_attributes=True)



class Page[T](BaseModel):
    """
    One page, and enough to draw a pager.

    Offset paging rather than a cursor. A console is the one place where
    jumping to page 7 and seeing "1,204 users" are both worth having, and the
    tables here are small enough that ``OFFSET`` never becomes the problem it
    is on a feed.
    """

    items: list[T]
    total: int
    limit: int
    offset: int

    @property
    def has_more(self) -> bool:
        return self.offset + len(self.items) < self.total


# --- Auth --------------------------------------------------------------------


class AdminLogin(BaseModel):
    email: str = Field(max_length=320, pattern=EMAIL_PATTERN)
    password: str = Field(min_length=1, max_length=256)


class AdminTokens(BaseModel):
    access_token: str
    refresh_token: str
    token_type: str = "bearer"
    expires_in: int
    admin: AdminOut


class RefreshRequest(BaseModel):
    refresh_token: str


class AdminOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    email: str
    full_name: str
    role: str
    is_active: bool
    last_login_at: datetime | None
    created_at: datetime


class AdminCreate(BaseModel):
    email: str = Field(max_length=320, pattern=EMAIL_PATTERN)
    password: str = Field(min_length=12, max_length=256)
    full_name: str = Field(default="", max_length=120)
    role: str = Field(default="support", pattern="^(support|admin|owner)$")


class AdminUpdate(BaseModel):
    full_name: str | None = Field(default=None, max_length=120)
    role: str | None = Field(default=None, pattern="^(support|admin|owner)$")
    is_active: bool | None = None
    #: Setting this revokes every session the admin has, including the one
    #: making the change if they are changing their own.
    password: str | None = Field(default=None, min_length=12, max_length=256)


# --- Overview ----------------------------------------------------------------


class UserCountsOut(FromDataclass):
    total: int
    deleted: int
    new_today: int
    new_7d: int
    new_30d: int
    with_devices: int


class PlanRowOut(FromDataclass):
    tier: str
    name: str
    price_ksh: int
    subscribers: int
    active: int
    paying: int
    unverified: int
    expiring_7d: int
    revenue_all_time_ksh: int
    revenue_30d_ksh: int
    mrr_ksh: int


class RevenueSummaryOut(FromDataclass):
    currency: str
    gross_ksh: int
    today_ksh: int
    last_7d_ksh: int
    last_30d_ksh: int
    previous_30d_ksh: int
    growth_30d_pct: float | None
    mrr_ksh: int
    arpu_ksh: int
    paying_customers: int
    successful_payments: int
    failed_payments: int
    pending_payments: int
    success_rate_pct: float
    average_payment_ksh: int
    by_channel: dict[str, int]


class FunnelOut(FromDataclass):
    signed_up: int
    started_trial: int
    trial_active: int
    trial_expired: int
    ever_paid: int
    paying_now: int
    trial_conversion_pct: float
    retention_pct: float


class OverviewOut(BaseModel):
    generated_at: datetime
    users: UserCountsOut
    revenue: RevenueSummaryOut
    plans: list[PlanRowOut]
    funnel: FunnelOut
    #: Things that want a human: unverified paid subscriptions, stalled
    #: extractions, plans about to lapse. The console shows these as a banner,
    #: which is the only reason they are on the overview rather than behind
    #: their own endpoint.
    attention: list[AttentionItem]


class AttentionItem(BaseModel):
    #: info | warn | critical
    level: str
    code: str
    message: str
    count: int
    #: Where the console should send someone who clicks it.
    link: str | None = None


class SeriesPointOut(FromDataclass):
    day: str
    value: int


class TimeseriesOut(BaseModel):
    metric: str
    days: int
    points: list[SeriesPointOut]
    total: int


# --- Users -------------------------------------------------------------------


class AdminSubscriptionOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    tier: str
    plan_name: str
    started_at: datetime
    expires_at: datetime | None
    verified: bool
    group_id: uuid.UUID | None
    days_remaining: int | None
    is_active: bool


class AdminUserRow(BaseModel):
    """One line in the users table. Deliberately not the full profile."""

    id: uuid.UUID
    phone: str | None
    email: str | None
    full_name: str
    institution: str
    created_at: datetime
    #: What is in force, after expiry has been applied — not the column.
    tier: str
    plan_name: str
    expires_at: datetime | None
    verified: bool
    is_deleted: bool
    total_paid_ksh: int


class AdminGroupSummary(BaseModel):
    id: uuid.UUID
    invite_code: str
    seats: int
    seats_taken: int
    expires_at: datetime | None
    is_owner: bool


class AdminPaymentRow(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    user_id: uuid.UUID
    reference: str
    tier: str
    amount_kes: int
    status: str
    channel: str | None
    paid_at: datetime | None
    created_at: datetime


class AdminDeviceRow(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    platform: str
    app_version: str
    #: Whether a push token exists, never the token itself. A console screen is
    #: not a reason to put a working push credential on someone's clipboard.
    has_push_token: bool
    is_active_device: bool
    created_at: datetime
    updated_at: datetime


class AdminUserDetail(BaseModel):
    id: uuid.UUID
    phone: str | None
    email: str | None
    full_name: str
    institution: str
    program: str
    year_of_study: int | None
    semester: int | None
    avatar_path: str | None
    created_at: datetime
    updated_at: datetime
    deleted_at: datetime | None

    subscription: AdminSubscriptionOut | None
    #: What the API would actually allow this account right now, from the same
    #: code path ``/billing/subscription`` uses. Shown next to the subscription
    #: row so support can see when the two disagree and why.
    effective_tier: str
    effective_plan_name: str

    activity: dict[str, int]
    usage: dict[str, dict[str, int]]
    limits: dict[str, object]
    payments: list[AdminPaymentRow]
    total_paid_ksh: int
    devices: list[AdminDeviceRow]
    groups: list[AdminGroupSummary]


class AdminUserUpdate(BaseModel):
    """A support edit. Every field optional; unset fields are left alone."""

    full_name: str | None = Field(default=None, max_length=120)
    email: str | None = Field(default=None, max_length=320)
    phone: str | None = Field(default=None, max_length=20)
    institution: str | None = Field(default=None, max_length=160)
    program: str | None = Field(default=None, max_length=160)
    year_of_study: int | None = Field(default=None, ge=1, le=8)
    semester: int | None = Field(default=None, ge=1, le=3)


class GrantSubscription(BaseModel):
    """
    Put an account on a plan by hand.

    The reason is required and not decorative: this endpoint moves money's
    worth of entitlement without money changing hands, and six months later
    "why is this account on Synapse for free" needs an answer that is not a
    guess.
    """

    tier: str = Field(pattern="^(trial|standard|pro|friends|expired)$")
    days: int | None = Field(
        default=None,
        ge=1,
        le=730,
        description="Length of the granted period. Defaults to the plan's own.",
    )
    #: True extends an existing period; False replaces it from today.
    extend: bool = True
    reason: str = Field(min_length=3, max_length=500)


class ResetUsage(BaseModel):
    metric: str | None = Field(
        default=None,
        description="One metric to clear. Omit to clear every counter.",
    )
    reason: str = Field(min_length=3, max_length=500)


class ActionResult(BaseModel):
    ok: bool = True
    message: str


# --- Subscriptions -----------------------------------------------------------


class AdminSubscriptionRow(BaseModel):
    id: uuid.UUID
    user_id: uuid.UUID
    full_name: str
    phone: str | None
    email: str | None
    tier: str
    plan_name: str
    started_at: datetime
    expires_at: datetime | None
    verified: bool
    is_active: bool
    days_remaining: int | None
    group_id: uuid.UUID | None


class SubscriptionStatsOut(BaseModel):
    generated_at: datetime
    plans: list[PlanRowOut]
    total_active: int
    total_paying: int
    total_trial: int
    total_expired: int
    #: Paid subscriptions the app wrote on a student's word that Paystack has
    #: never confirmed. Each is either a payment that went missing or a plan
    #: nobody paid for.
    total_unverified: int
    mrr_ksh: int


# --- Groups ------------------------------------------------------------------


class AdminGroupMember(BaseModel):
    user_id: uuid.UUID
    full_name: str
    phone: str | None
    is_owner: bool
    joined_at: datetime


class AdminGroupRow(BaseModel):
    id: uuid.UUID
    owner_id: uuid.UUID
    owner_name: str
    owner_phone: str | None
    tier: str
    invite_code: str
    seats: int
    seats_taken: int
    expires_at: datetime | None
    is_active: bool
    created_at: datetime


class AdminGroupDetail(AdminGroupRow):
    members: list[AdminGroupMember]


# --- Content -----------------------------------------------------------------


class ContentStatsOut(FromDataclass):
    units: int
    materials: int
    material_chunks: int
    storage_bytes: int
    events: int
    class_sessions: int
    chats: int
    messages: int
    tutor_answers: int
    prompt_tokens: int
    completion_tokens: int
    extraction: dict[str, int]
    extraction_stalled: int


class AdminMaterialRow(BaseModel):
    id: uuid.UUID
    user_id: uuid.UUID
    unit_id: uuid.UUID
    kind: str
    title: str
    byte_size: int | None
    page_count: int | None
    extraction_status: str
    extraction_error: str | None
    created_at: datetime


# --- Ops ---------------------------------------------------------------------


class OpsHealthOut(BaseModel):
    environment: str
    database_ok: bool
    database_latency_ms: float
    #: Which integrations actually have credentials. A console that shows
    #: revenue while Paystack is unconfigured is showing history, not a
    #: business, and this row is what says so.
    integrations: dict[str, bool]
    counts: dict[str, int]
    warnings: list[str]


# --- Audit -------------------------------------------------------------------


class AuditRow(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    admin_id: uuid.UUID | None
    admin_email: str
    action: str
    target_type: str
    target_id: uuid.UUID | None
    summary: str
    meta: dict | None
    ip: str | None
    created_at: datetime


# Deferred annotations: OverviewOut names AttentionItem before it is defined,
# and AdminTokens names AdminOut. Both are resolved once the module is built.
OverviewOut.model_rebuild()
AdminTokens.model_rebuild()
