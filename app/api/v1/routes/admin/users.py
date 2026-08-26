import uuid
from dataclasses import asdict
from datetime import timedelta

from fastapi import APIRouter, Query
from sqlalchemy import delete, func, or_, select

from app.api.deps import AdminRole, ClientIp, CurrentAdmin, DbSession
from app.core.clock import as_utc
from app.core.clock import now as utc_now
from app.core.errors import AppError, NotFound
from app.models.account import Device, User
from app.models.billing import (
    Payment,
    PlanGroupMember,
    Subscription,
    UsageCounter,
)
from app.schemas.admin import (
    ActionResult,
    AdminDeviceRow,
    AdminGroupSummary,
    AdminPaymentRow,
    AdminSubscriptionOut,
    AdminUserDetail,
    AdminUserRow,
    AdminUserUpdate,
    GrantSubscription,
    Page,
    ResetUsage,
)
from app.services import analytics
from app.services import audit as audit_service
from app.services import billing as billing_service
from app.services.plans import Tier, plan_for
from app.services.quota import METRIC_PERIODS, get_entitlement, new_period_end

router = APIRouter()


def _days_remaining(expires_at) -> int | None:
    expires = as_utc(expires_at)
    if expires is None:
        return None
    return max(0, (expires - utc_now()).days)


def _subscription_out(subscription: Subscription | None) -> AdminSubscriptionOut | None:
    if subscription is None:
        return None

    expires = as_utc(subscription.expires_at)
    return AdminSubscriptionOut(
        tier=subscription.tier,
        plan_name=plan_for(subscription.tier).name,
        started_at=subscription.started_at,
        expires_at=subscription.expires_at,
        verified=subscription.verified,
        group_id=subscription.group_id,
        days_remaining=_days_remaining(subscription.expires_at),
        is_active=bool(
            subscription.verified and expires is not None and expires > utc_now()
        ),
    )


@router.get("", response_model=Page[AdminUserRow], summary="Search users")
async def list_users(
    session: DbSession,
    q: str | None = Query(
        default=None,
        description="Matches phone, email, name or institution. "
        "A full uuid matches the account id exactly.",
    ),
    tier: str | None = Query(default=None, description="trial | standard | pro | friends"),
    status: str | None = Query(
        default=None,
        description="active | expired | unverified | trial | paying | deleted",
    ),
    institution: str | None = None,
    sort: str = Query(
        default="created_at", description="created_at | expires_at | name"
    ),
    order: str = Query(default="desc", pattern="^(asc|desc)$"),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
) -> Page[AdminUserRow]:
    """
    The users table.

    Soft-deleted accounts are excluded unless ``status=deleted`` asks for them.
    Every other view in the console counts live accounts, and a list that
    quietly includes tombstones is a list whose total never matches the
    dashboard.

    A single left join to ``subscriptions`` rather than a query per row: fifty
    rows means fifty round trips otherwise, which is the difference between a
    table that opens and one that hangs. The payment total is a second grouped
    query over just the ids on this page, for the same reason.
    """
    now = utc_now()
    statement = select(User, Subscription).outerjoin(
        Subscription, Subscription.user_id == User.id
    )

    if status == "deleted":
        statement = statement.where(User.deleted_at.isnot(None))
    else:
        statement = statement.where(User.deleted_at.is_(None))

    if q:
        needle = q.strip()
        try:
            # A pasted uuid is almost always someone chasing one specific
            # account from a log line, so it is matched exactly rather than
            # being dropped into a LIKE that would never hit.
            statement = statement.where(User.id == uuid.UUID(needle))
        except ValueError:
            pattern = f"%{needle.lower()}%"
            statement = statement.where(
                or_(
                    func.lower(User.phone).like(pattern),
                    func.lower(User.email).like(pattern),
                    func.lower(User.full_name).like(pattern),
                    func.lower(User.institution).like(pattern),
                )
            )

    if tier:
        statement = statement.where(Subscription.tier == tier)

    if institution:
        statement = statement.where(
            func.lower(User.institution) == institution.strip().lower()
        )

    live = (
        Subscription.verified.is_(True),
        Subscription.expires_at.isnot(None),
        Subscription.expires_at > now,
    )
    if status == "active":
        statement = statement.where(*live)
    elif status == "paying":
        statement = statement.where(
            Subscription.tier.in_(analytics.paid_tiers()), *live
        )
    elif status == "trial":
        statement = statement.where(Subscription.tier == Tier.TRIAL.value, *live)
    elif status == "expired":
        statement = statement.where(
            or_(
                Subscription.id.is_(None),
                Subscription.expires_at.is_(None),
                Subscription.expires_at <= now,
            )
        )
    elif status == "unverified":
        statement = statement.where(
            Subscription.tier.in_(analytics.paid_tiers()),
            Subscription.verified.is_(False),
        )

    total = (
        await session.scalar(
            select(func.count()).select_from(statement.subquery())
        )
    ) or 0

    column = {
        "created_at": User.created_at,
        "expires_at": Subscription.expires_at,
        "name": User.full_name,
    }.get(sort, User.created_at)
    statement = statement.order_by(
        column.desc() if order == "desc" else column.asc()
    ).limit(limit).offset(offset)

    rows = (await session.execute(statement)).all()

    paid_by_user: dict[uuid.UUID, int] = {}
    if rows:
        ids = [user.id for user, _ in rows]
        paid_by_user = {
            user_id: int(total_paid or 0)
            for user_id, total_paid in (
                await session.execute(
                    select(
                        Payment.user_id,
                        func.coalesce(func.sum(Payment.amount_kes), 0),
                    )
                    .where(Payment.user_id.in_(ids), Payment.status == "success")
                    .group_by(Payment.user_id)
                )
            ).all()
        }

    items = []
    for user, subscription in rows:
        expires = as_utc(subscription.expires_at) if subscription else None
        lapsed = expires is None or expires <= now
        effective = (
            Tier.EXPIRED.value
            if subscription is None or lapsed or not subscription.verified
            else subscription.tier
        )
        items.append(
            AdminUserRow(
                id=user.id,
                phone=user.phone,
                email=user.email,
                full_name=user.full_name,
                institution=user.institution,
                created_at=user.created_at,
                tier=effective,
                plan_name=plan_for(effective).name,
                expires_at=subscription.expires_at if subscription else None,
                verified=bool(subscription and subscription.verified),
                is_deleted=user.deleted_at is not None,
                total_paid_ksh=paid_by_user.get(user.id, 0),
            )
        )

    return Page(items=items, total=total, limit=limit, offset=offset)


async def _load(session, user_id: uuid.UUID) -> User:
    user = await session.get(User, user_id)
    if user is None:
        raise NotFound("No account with that id.")
    return user


@router.get("/{user_id}", response_model=AdminUserDetail, summary="One user in full")
async def get_user(user_id: uuid.UUID, session: DbSession) -> AdminUserDetail:
    """
    Everything about one account, in one response.

    Support answers a ticket from this screen, and a ticket is almost never
    about one fact — "my plan says Synapse but it will not let me ask
    questions" needs the subscription row, the *effective* entitlement and the
    usage counters side by side. Fetching them separately means three screens
    and a guess about which one is stale.

    ``effective_tier`` comes from the same ``get_entitlement`` the mobile API
    uses. When it disagrees with the subscription row — unverified payment,
    lapsed period — that disagreement is the answer to the ticket.
    """
    user = await _load(session, user_id)

    subscription = await session.scalar(
        select(Subscription).where(Subscription.user_id == user_id)
    )
    entitlement = await get_entitlement(session, user_id)

    payments = (
        await session.scalars(
            select(Payment)
            .where(Payment.user_id == user_id)
            .order_by(Payment.created_at.desc())
            .limit(50)
        )
    ).all()

    total_paid = int(
        await session.scalar(
            select(func.coalesce(func.sum(Payment.amount_kes), 0)).where(
                Payment.user_id == user_id, Payment.status == "success"
            )
        )
        or 0
    )

    devices = (
        await session.scalars(
            select(Device)
            .where(Device.user_id == user_id)
            .order_by(Device.updated_at.desc())
        )
    ).all()

    groups = []
    for group, is_owner in await analytics.group_membership(session, user_id):
        groups.append(
            AdminGroupSummary(
                id=group.id,
                invite_code=group.invite_code,
                seats=group.seats,
                seats_taken=await billing_service.seats_taken(session, group.id),
                expires_at=group.expires_at,
                is_owner=is_owner,
            )
        )

    return AdminUserDetail(
        id=user.id,
        phone=user.phone,
        email=user.email,
        full_name=user.full_name,
        institution=user.institution,
        program=user.program,
        year_of_study=user.year_of_study,
        semester=user.semester,
        avatar_path=user.avatar_path,
        created_at=user.created_at,
        updated_at=user.updated_at,
        deleted_at=user.deleted_at,
        subscription=_subscription_out(subscription),
        effective_tier=entitlement.tier.value,
        effective_plan_name=plan_for(entitlement.tier).name,
        activity=await analytics.user_activity(session, user_id),
        usage=await analytics.usage_rows(session, user_id),
        limits=asdict(entitlement.limits),
        payments=[AdminPaymentRow.model_validate(row) for row in payments],
        total_paid_ksh=total_paid,
        devices=[
            AdminDeviceRow(
                id=device.id,
                platform=device.platform,
                app_version=device.app_version,
                has_push_token=bool(device.push_token),
                is_active_device=user.active_device_id == device.id,
                created_at=device.created_at,
                updated_at=device.updated_at,
            )
            for device in devices
        ],
        groups=groups,
    )


@router.patch("/{user_id}", response_model=AdminUserDetail, summary="Edit a profile")
async def update_user(
    user_id: uuid.UUID,
    body: AdminUserUpdate,
    session: DbSession,
    admin: CurrentAdmin,
    ip: ClientIp,
) -> AdminUserDetail:
    """
    A support edit — fixing a mistyped name or attaching an email.

    ``exclude_unset`` is what makes this a patch rather than a replace: without
    it, a console form that only sends the name would blank the institution and
    the program alongside it.

    The before-values go into the audit entry. An audit log that records only
    what a field became cannot answer "what did we change it from", which is
    the question that gets asked.
    """
    user = await _load(session, user_id)
    changes = body.model_dump(exclude_unset=True)

    if not changes:
        raise AppError("Nothing to change.")

    before = {field: getattr(user, field) for field in changes}
    for field, value in changes.items():
        setattr(user, field, value)

    await session.flush()
    await audit_service.record(
        session,
        admin=admin,
        action="user.updated",
        target_type="user",
        target_id=user.id,
        summary=f"Edited {', '.join(sorted(changes))} on {user.full_name or user.id}",
        meta={
            "before": {key: str(value) for key, value in before.items()},
            "after": {key: str(value) for key, value in changes.items()},
        },
        ip=ip,
    )

    return await get_user(user_id, session)


@router.post(
    "/{user_id}/subscription",
    response_model=AdminSubscriptionOut,
    summary="Grant or change a plan",
)
async def grant_subscription(
    user_id: uuid.UUID,
    body: GrantSubscription,
    session: DbSession,
    admin: AdminRole,
    ip: ClientIp,
) -> AdminSubscriptionOut:
    """
    Put an account on a plan without a payment.

    This is the endpoint that hands out money's worth of product, so it is
    gated at ``admin`` rather than ``support`` and the reason is mandatory. A
    granted plan is marked ``verified`` — it is genuinely entitled, it just was
    not bought — and the audit entry is the only record that says why.

    ``extend`` follows the same rule as a real renewal: extending the tier
    already held adds to the remaining period instead of restarting it, so a
    goodwill week given to someone with ten days left leaves them seventeen,
    not seven.
    """
    user = await _load(session, user_id)
    tier = Tier(body.tier)

    subscription = await billing_service.get_or_create_subscription(session, user_id)
    before = {
        "tier": subscription.tier,
        "expires_at": str(subscription.expires_at),
        "verified": subscription.verified,
    }

    now = utc_now()
    current_end = as_utc(subscription.expires_at)
    same_tier = subscription.tier == tier.value
    still_live = current_end is not None and current_end > now
    base = current_end if (body.extend and same_tier and still_live) else now

    subscription.tier = tier.value
    subscription.started_at = subscription.started_at if same_tier else now
    subscription.expires_at = (
        base + timedelta(days=body.days) if body.days else new_period_end(tier, base)
    )
    # A comped plan is not a group seat. Clearing this keeps the Friends seat
    # accounting honest — otherwise a granted plan looks like a paid group's
    # member and the group's revenue attribution silently gains a person.
    subscription.group_id = None
    subscription.verified = tier is not Tier.EXPIRED

    if tier is Tier.EXPIRED:
        subscription.expires_at = now

    await session.flush()
    await audit_service.record(
        session,
        admin=admin,
        action="subscription.granted",
        target_type="user",
        target_id=user.id,
        summary=(
            f"Granted {plan_for(tier).name} to {user.full_name or user.phone or user.id}"
            f" — {body.reason}"
        ),
        meta={
            "before": before,
            "after": {
                "tier": subscription.tier,
                "expires_at": str(subscription.expires_at),
                "verified": subscription.verified,
            },
            "reason": body.reason,
            "days": body.days,
            "extend": body.extend,
        },
        ip=ip,
    )

    return _subscription_out(subscription)


@router.delete(
    "/{user_id}/subscription",
    response_model=ActionResult,
    summary="End a plan now",
)
async def revoke_subscription(
    user_id: uuid.UUID,
    session: DbSession,
    admin: AdminRole,
    ip: ClientIp,
    reason: str = Query(min_length=3, max_length=500),
) -> ActionResult:
    """
    Expires a subscription immediately.

    The row is expired rather than deleted. A deleted subscription reads as
    "never had one", which the trial logic treats as a returning abuser, and it
    also erases the history of what this account was on — both of which are
    worse than a row with a past date.
    """
    user = await _load(session, user_id)
    subscription = await session.scalar(
        select(Subscription).where(Subscription.user_id == user_id)
    )
    if subscription is None:
        raise NotFound("That account has no subscription.")

    before = {"tier": subscription.tier, "expires_at": str(subscription.expires_at)}
    subscription.expires_at = utc_now()
    subscription.verified = False
    await session.flush()

    await audit_service.record(
        session,
        admin=admin,
        action="subscription.revoked",
        target_type="user",
        target_id=user.id,
        summary=(
            f"Ended {plan_for(before['tier']).name} for "
            f"{user.full_name or user.id} — {reason}"
        ),
        meta={"before": before, "reason": reason},
        ip=ip,
    )

    return ActionResult(message="Subscription ended.")


@router.post(
    "/{user_id}/device-reset",
    response_model=ActionResult,
    summary="Release the device lock",
)
async def reset_device(
    user_id: uuid.UUID,
    session: DbSession,
    admin: CurrentAdmin,
    ip: ClientIp,
    reason: str = Query(default="New device", max_length=500),
) -> ActionResult:
    """
    Clears ``active_device_id`` so the account can sign in somewhere new.

    The single most common support request this product will get. One device
    per account is enforced on every request, so a lost or replaced phone locks
    a paying student out with no self-service way back — and telling them to
    wait for a token to expire does not help, because the lock is on the
    account, not the token.

    Available to ``support``: it grants nothing, it only unblocks someone who
    already paid.
    """
    user = await _load(session, user_id)
    previous = user.active_device_id
    user.active_device_id = None
    await session.flush()

    await audit_service.record(
        session,
        admin=admin,
        action="user.device_reset",
        target_type="user",
        target_id=user.id,
        summary=(
            f"Released device lock for "
            f"{user.full_name or user.phone or user.id} — {reason}"
        ),
        meta={
            "previous_device_id": str(previous) if previous else None,
            "reason": reason,
        },
        ip=ip,
    )

    return ActionResult(message="Device lock released. They can sign in again.")


@router.get("/{user_id}/usage", summary="Usage counters")
async def get_usage(user_id: uuid.UUID, session: DbSession) -> dict:
    """
    Every counter, plus what the current period allows.

    The limits are included because a bare number means nothing — "38" is
    either fine or the reason nothing works, depending on a plan the caller
    would otherwise have to look up separately.
    """
    await _load(session, user_id)
    entitlement = await get_entitlement(session, user_id)

    return {
        "tier": entitlement.tier.value,
        "plan_name": plan_for(entitlement.tier).name,
        "current_periods": {
            metric: period() for metric, period in METRIC_PERIODS.items()
        },
        "counters": await analytics.usage_rows(session, user_id),
        "limits": asdict(entitlement.limits),
    }


@router.post(
    "/{user_id}/usage/reset", response_model=ActionResult, summary="Clear counters"
)
async def reset_usage(
    user_id: uuid.UUID,
    body: ResetUsage,
    session: DbSession,
    admin: AdminRole,
    ip: ClientIp,
) -> ActionResult:
    """
    Deletes usage rows so a student gets their allowance back.

    Rows are deleted rather than zeroed. A counter at zero and a counter that
    does not exist mean the same thing to ``current_usage``, and deleting keeps
    the table from accumulating a row per metric per period per reset forever.

    Gated at ``admin`` because handing back a day's AI questions is handing
    back something the plan sells.
    """
    await _load(session, user_id)

    statement = delete(UsageCounter).where(UsageCounter.user_id == user_id)
    if body.metric:
        if body.metric not in METRIC_PERIODS:
            raise AppError(
                f"Unknown metric. Try one of: {', '.join(sorted(METRIC_PERIODS))}."
            )
        statement = statement.where(UsageCounter.metric == body.metric)

    result = await session.execute(statement)
    cleared = result.rowcount or 0

    await audit_service.record(
        session,
        admin=admin,
        action="usage.reset",
        target_type="user",
        target_id=user_id,
        summary=(
            f"Cleared {body.metric or 'all'} usage counters "
            f"({cleared} row{'' if cleared == 1 else 's'}) — {body.reason}"
        ),
        meta={"metric": body.metric, "rows": cleared, "reason": body.reason},
        ip=ip,
    )

    return ActionResult(message=f"Cleared {cleared} counter rows.")


@router.delete("/{user_id}", response_model=ActionResult, summary="Delete an account")
async def delete_user(
    user_id: uuid.UUID,
    session: DbSession,
    admin: AdminRole,
    ip: ClientIp,
    reason: str = Query(min_length=3, max_length=500),
) -> ActionResult:
    """
    Soft-deletes, exactly as the student's own ``DELETE /me`` does.

    Not a hard delete, and not because it is easier: a device that has been
    offline never hears about a row that simply vanished, and pushes it back on
    the next sync. A ``deleted_at`` propagates. It is also reversible, which
    matters when the request came through a support channel and the person on
    the other end may not have been the account holder.
    """
    user = await _load(session, user_id)
    if user.deleted_at is not None:
        raise AppError("That account is already deleted.")

    user.deleted_at = utc_now()
    user.active_device_id = None
    await session.flush()

    await audit_service.record(
        session,
        admin=admin,
        action="user.deleted",
        target_type="user",
        target_id=user.id,
        summary=f"Deleted {user.full_name or user.phone or user.id} — {reason}",
        meta={"reason": reason, "phone": user.phone, "email": user.email},
        ip=ip,
    )

    return ActionResult(message="Account deleted.")


@router.post(
    "/{user_id}/restore", response_model=ActionResult, summary="Undo a deletion"
)
async def restore_user(
    user_id: uuid.UUID,
    session: DbSession,
    admin: AdminRole,
    ip: ClientIp,
    reason: str = Query(min_length=3, max_length=500),
) -> ActionResult:
    """
    Clears the tombstone.

    The trial grant is deliberately *not* restored or re-issued — see
    ``app/models/trial.py``. A restored account gets its data back, not another
    fourteen free days.
    """
    user = await _load(session, user_id)
    if user.deleted_at is None:
        raise AppError("That account is not deleted.")

    user.deleted_at = None
    await session.flush()

    await audit_service.record(
        session,
        admin=admin,
        action="user.restored",
        target_type="user",
        target_id=user.id,
        summary=f"Restored {user.full_name or user.phone or user.id} — {reason}",
        meta={"reason": reason},
        ip=ip,
    )

    return ActionResult(message="Account restored.")


@router.get("/{user_id}/groups", summary="Friends plans this student is on")
async def user_groups(user_id: uuid.UUID, session: DbSession) -> list[dict]:
    await _load(session, user_id)

    out = []
    for group, is_owner in await analytics.group_membership(session, user_id):
        members = (
            await session.execute(
                select(PlanGroupMember.user_id, User.full_name)
                .join(User, User.id == PlanGroupMember.user_id)
                .where(PlanGroupMember.group_id == group.id)
            )
        ).all()
        out.append(
            {
                "id": str(group.id),
                "invite_code": group.invite_code,
                "seats": group.seats,
                "seats_taken": len(members),
                "expires_at": group.expires_at,
                "is_owner": is_owner,
                "members": [
                    {"user_id": str(member_id), "full_name": name}
                    for member_id, name in members
                ],
            }
        )

    return out
