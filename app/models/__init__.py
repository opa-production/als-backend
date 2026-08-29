"""
Every table, imported in one place.

Alembic's ``--autogenerate`` compares the database against
``Base.metadata``, and a model class only registers itself there when its
module is imported. A table missing from this file is a table Alembic will
cheerfully generate a migration to *drop*.
"""

from app.db.base import Base
from app.models.account import Device, User
from app.models.admin import AdminAuditLog, AdminRefreshToken, AdminUser
from app.models.auth import OtpCode, RefreshToken
from app.models.billing import (
    Payment,
    PlanGroup,
    PlanGroupMember,
    Subscription,
    UsageCounter,
)
from app.models.course import ClassSession, Unit
from app.models.knowledge import Material, MaterialChunk
from app.models.notification import NotificationLog
from app.models.planner import Event
from app.models.settings import StudyDay, UserSettings
from app.models.trial import TrialGrant
from app.models.tutor import Chat, Message

__all__ = [
    "AdminAuditLog",
    "AdminRefreshToken",
    "AdminUser",
    "Base",
    "ClassSession",
    "Chat",
    "Device",
    "Event",
    "Material",
    "MaterialChunk",
    "Message",
    "NotificationLog",
    "OtpCode",
    "Payment",
    "PlanGroup",
    "PlanGroupMember",
    "RefreshToken",
    "StudyDay",
    "Subscription",
    "TrialGrant",
    "Unit",
    "UsageCounter",
    "User",
    "UserSettings",
]
