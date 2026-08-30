"""
The admin console API.

Mounted as one router so that everything under it shares a prefix and a tag
group in Swagger, and so there is exactly one place to look when asking "what
can an administrator do".

Every route here requires an admin token. That is enforced per router below
rather than per handler: a dependency listed once cannot be forgotten on the
handler someone adds next month, which is the way this kind of surface
actually leaks.
"""

from fastapi import APIRouter, Depends

from app.api.deps import get_current_admin
from app.api.v1.routes.admin import (
    admins,
    audit,
    auth,
    content,
    feedback,
    groups,
    ops,
    overview,
    payments,
    referrals,
    revenue,
    subscriptions,
    users,
)

admin_router = APIRouter()

# Unauthenticated by necessity — this is where a token comes from.
admin_router.include_router(auth.router, prefix="/auth", tags=["admin: auth"])

#: Everything else. The dependency is attached to the router, not repeated on
#: forty handlers.
guarded = APIRouter(dependencies=[Depends(get_current_admin)])

guarded.include_router(overview.router, tags=["admin: overview"])
guarded.include_router(users.router, prefix="/users", tags=["admin: users"])
guarded.include_router(
    subscriptions.router, prefix="/subscriptions", tags=["admin: subscriptions"]
)
guarded.include_router(revenue.router, prefix="/revenue", tags=["admin: revenue"])
guarded.include_router(payments.router, prefix="/payments", tags=["admin: revenue"])
guarded.include_router(groups.router, prefix="/groups", tags=["admin: groups"])
guarded.include_router(content.router, prefix="/content", tags=["admin: content"])
guarded.include_router(
    feedback.router, prefix="/feedback", tags=["admin: feedback"]
)
guarded.include_router(
    referrals.router, prefix="/referrals", tags=["admin: referrals"]
)
guarded.include_router(ops.router, prefix="/ops", tags=["admin: ops"])
guarded.include_router(audit.router, prefix="/audit", tags=["admin: audit"])
guarded.include_router(admins.router, prefix="/admins", tags=["admin: admins"])

admin_router.include_router(guarded)

__all__ = ["admin_router"]
