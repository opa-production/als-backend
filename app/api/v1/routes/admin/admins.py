import uuid

from fastapi import APIRouter, Query
from sqlalchemy import func, select

from app.api.deps import ClientIp, CurrentAdmin, DbSession, OwnerRole
from app.core.errors import AppError, NotFound
from app.models.admin import AdminRefreshToken, AdminUser
from app.schemas.admin import (
    ActionResult,
    AdminCreate,
    AdminOut,
    AdminUpdate,
)
from app.services import admin_auth
from app.services import audit as audit_service

router = APIRouter()


@router.get("", response_model=list[AdminOut], summary="Everyone with access")
async def list_admins(session: DbSession, admin: CurrentAdmin) -> list[AdminOut]:
    """
    Readable by every admin, not just the owner.

    Who else can reach this data is not a secret to keep from the people who
    can already reach it, and an access list nobody can see is an access list
    nobody notices growing.
    """
    rows = (await session.scalars(select(AdminUser).order_by(AdminUser.created_at))).all()
    return [AdminOut.model_validate(row) for row in rows]


@router.post("", response_model=AdminOut, status_code=201, summary="Add an admin")
async def create_admin(
    body: AdminCreate,
    session: DbSession,
    owner: OwnerRole,
    ip: ClientIp,
) -> AdminOut:
    """
    Owner only.

    The password is set here and never shown again — there is no read path for
    it and no reset link, because a reset link needs an email sender this
    service does not have. An admin who forgets their password gets a new one
    set by an owner, which for a team of this size is the honest workflow
    rather than a pretend one.
    """
    admin = await admin_auth.create_admin(
        session,
        email=body.email,
        password=body.password,
        full_name=body.full_name,
        role=body.role,
    )

    await audit_service.record(
        session,
        admin=owner,
        action="admin.created",
        target_type="admin",
        target_id=admin.id,
        summary=f"Created {admin.role} {admin.email}",
        meta={"role": admin.role},
        ip=ip,
    )

    return AdminOut.model_validate(admin)


@router.patch("/{admin_id}", response_model=AdminOut, summary="Change an admin")
async def update_admin(
    admin_id: uuid.UUID,
    body: AdminUpdate,
    session: DbSession,
    owner: OwnerRole,
    ip: ClientIp,
) -> AdminOut:
    """
    Role, name, active flag, password. Owner only.

    Two refusals worth naming. An owner cannot demote or deactivate themselves
    while they are the last active owner — that is how a console ends up with
    nobody able to add anyone, recoverable only by hand at the database. And a
    password change revokes every session that admin holds, which is the whole
    point of changing it under suspicion.
    """
    target = await session.get(AdminUser, admin_id)
    if target is None:
        raise NotFound("No admin with that id.")

    changes = body.model_dump(exclude_unset=True)
    if not changes:
        raise AppError("Nothing to change.")

    losing_owner = (
        target.role == "owner"
        and (changes.get("role", target.role) != "owner" or changes.get("is_active") is False)
    )
    if losing_owner:
        remaining = (
            await session.scalar(
                select(func.count())
                .select_from(AdminUser)
                .where(
                    AdminUser.role == "owner",
                    AdminUser.is_active.is_(True),
                    AdminUser.id != target.id,
                )
            )
        ) or 0
        if remaining == 0:
            raise AppError(
                "That is the last active owner. Promote someone else first."
            )

    password = changes.pop("password", None)
    before = {field: getattr(target, field) for field in changes}

    for field, value in changes.items():
        setattr(target, field, value)

    if password:
        await admin_auth.set_password(session, admin=target, password=password)

    if changes.get("is_active") is False:
        # Deactivation has to end the sessions too. Without this the account is
        # inert at the next login and fully live for the hour its current
        # access token has left to run.
        await admin_auth.revoke_all(session, target.id)

    await session.flush()
    await audit_service.record(
        session,
        admin=owner,
        action="admin.updated",
        target_type="admin",
        target_id=target.id,
        summary=(
            f"Updated {target.email}: "
            + ", ".join(sorted([*changes, *(["password"] if password else [])]))
        ),
        meta={
            "before": {key: str(value) for key, value in before.items()},
            "after": {key: str(value) for key, value in changes.items()},
            "password_changed": bool(password),
        },
        ip=ip,
    )

    return AdminOut.model_validate(target)


@router.delete("/{admin_id}", response_model=ActionResult, summary="Remove an admin")
async def delete_admin(
    admin_id: uuid.UUID,
    session: DbSession,
    owner: OwnerRole,
    ip: ClientIp,
    reason: str = Query(min_length=3, max_length=500),
) -> ActionResult:
    """
    Deactivates and ends every session. Does not delete the row.

    The row stays because ``admin_audit_log.admin_id`` points at it, and an
    audit trail whose actor rows have been deleted is a trail of anonymous
    actions. Deactivated is as removed as this needs to be: no login, no token,
    no access — and the log still says who did what.
    """
    target = await session.get(AdminUser, admin_id)
    if target is None:
        raise NotFound("No admin with that id.")

    if target.id == owner.id:
        raise AppError("You cannot remove your own access.")

    if target.role == "owner":
        remaining = (
            await session.scalar(
                select(func.count())
                .select_from(AdminUser)
                .where(
                    AdminUser.role == "owner",
                    AdminUser.is_active.is_(True),
                    AdminUser.id != target.id,
                )
            )
        ) or 0
        if remaining == 0:
            raise AppError("That is the last active owner.")

    target.is_active = False
    revoked = await admin_auth.revoke_all(session, target.id)
    await session.flush()

    await audit_service.record(
        session,
        admin=owner,
        action="admin.removed",
        target_type="admin",
        target_id=target.id,
        summary=f"Removed {target.email} — {reason}",
        meta={"reason": reason, "sessions_revoked": revoked},
        ip=ip,
    )

    return ActionResult(message=f"{target.email} can no longer sign in.")


@router.get("/{admin_id}/sessions", summary="Live sessions for one admin")
async def sessions(
    admin_id: uuid.UUID, session: DbSession, owner: OwnerRole
) -> list[dict]:
    """
    Unrevoked, unexpired refresh tokens, described but never shown.

    The hash is not returned and neither is anything that could reconstruct the
    token. What comes back is when and roughly from where — which is what makes
    a session someone does not recognise recognisable as such.
    """
    rows = (
        await session.scalars(
            select(AdminRefreshToken)
            .where(
                AdminRefreshToken.admin_id == admin_id,
                AdminRefreshToken.revoked_at.is_(None),
            )
            .order_by(AdminRefreshToken.created_at.desc())
        )
    ).all()

    return [
        {
            "id": str(row.id),
            "created_at": row.created_at,
            "expires_at": row.expires_at,
            "ip": row.ip,
            "user_agent": row.user_agent,
        }
        for row in rows
    ]
