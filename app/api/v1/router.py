from fastapi import APIRouter

from app.api.v1.routes import auth, billing, materials, me, settings, sync

api_router = APIRouter()

api_router.include_router(auth.router, prefix="/auth", tags=["auth"])
api_router.include_router(me.router, prefix="/me", tags=["account"])
# Mounted under /me because every one of these is a property of the account.
api_router.include_router(settings.router, prefix="/me", tags=["account"])
api_router.include_router(sync.router, prefix="/sync", tags=["sync"])
api_router.include_router(materials.router, prefix="/materials", tags=["knowledge"])
api_router.include_router(billing.router, prefix="/billing", tags=["billing"])

# Still to come — the complex tier, per ROADMAP.md.
#
#   tutor       POST /tutor/ask       generate over passages the device ranked
#               POST /tutor/quiz
#
# Everything there depends on the extraction pipeline: PDF text, OCR and
# chunking all have to work before an answer can cite a page.
#
# from app.api.v1.routes import tutor
# api_router.include_router(tutor.router, prefix="/tutor", tags=["tutor"])
