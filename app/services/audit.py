from __future__ import annotations

import uuid

import structlog
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.admin import AdminAuditLog, AdminUser

log = structlog.get_logger()


async def record(
    session: AsyncSession,
    *,
    admin: AdminUser | None,
    action: str,
    summary: str,
    target_type: str = "",
    target_id: uuid.UUID | None = None,
    meta: dict | None = None,
    ip: str | None = None,
) -> AdminAuditLog:
    """
    Appends one entry, in the caller's transaction.

    Deliberately not committed here. The entry lands when the change it
    describes lands, and is rolled back with it — a log that records actions
    which did not happen is worse than no log, because it is believed.
    """
    entry = AdminAuditLog(
        admin_id=admin.id if admin else None,
        admin_email=admin.email if admin else "",
        action=action,
        target_type=target_type,
        target_id=target_id,
        summary=summary,
        meta=meta,
        ip=ip,
    )
    session.add(entry)
    await session.flush()

    log.info(
        "admin_action",
        action=action,
        admin=admin.email if admin else None,
        target=str(target_id) if target_id else None,
    )
    return entry
