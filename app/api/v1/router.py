from fastapi import APIRouter

from app.api.v1.routes import (
    app_release,
    auth,
    billing,
    feedback,
    materials,
    me,
    settings,
    sync,
    tutor,
)
from app.api.v1.routes.admin import admin_router

api_router = APIRouter()

api_router.include_router(auth.router, prefix="/auth", tags=["auth"])

# Unauthenticated, and first because it is the first thing the app calls. A
# build too broken to sign in still has to be able to find out it is too old.
api_router.include_router(app_release.router, prefix="/app", tags=["app"])
api_router.include_router(me.router, prefix="/me", tags=["account"])
# Mounted under /me because every one of these is a property of the account.
api_router.include_router(settings.router, prefix="/me", tags=["account"])
api_router.include_router(feedback.router, prefix="/me", tags=["account"])
api_router.include_router(sync.router, prefix="/sync", tags=["sync"])
api_router.include_router(materials.router, prefix="/materials", tags=["knowledge"])
api_router.include_router(billing.router, prefix="/billing", tags=["billing"])

# The tutor. `/ask` streams server-sent events rather than returning JSON — see
# the note in its module for why buffering a six-second answer is the wrong
# trade. Retrieval happens here rather than on the device, because deciding
# "your notes do not cover this" is only trustworthy if the server did the
# looking.
api_router.include_router(tutor.router, prefix="/tutor", tags=["tutor"])

# The console. Its own auth, its own token type, its own audience — see
# app/api/v1/routes/admin/__init__.py. Mounted last because nothing else
# depends on it and because a route table reads better with the product
# above the back office.
api_router.include_router(admin_router, prefix="/admin")
