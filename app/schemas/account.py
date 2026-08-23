import uuid
from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field


class SubscriptionOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    tier: str
    started_at: datetime
    expires_at: datetime | None
    #: False until a payment webhook has confirmed the money. The app writes an
    #: unverified subscription on a student's word; this is the truth.
    verified: bool


class ProfileOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    phone: str | None
    email: str | None
    full_name: str
    institution: str
    program: str
    year_of_study: int | None
    semester: int | None

    #: A path, not a URL. Signed download URLs are minted per request and
    #: expire, so storing one in a profile response would hand out a link that
    #: stops working.
    avatar_path: str | None

    created_at: datetime
    subscription: SubscriptionOut | None = None


class ProfileUpdate(BaseModel):
    """
    A patch. Every field optional, and unset fields are left alone.

    ``exclude_unset`` on the caller's side is what makes that true — without
    it, a client sending only a name would blank the institution.
    """

    full_name: str | None = Field(default=None, max_length=120)
    email: str | None = Field(default=None, max_length=320)
    institution: str | None = Field(default=None, max_length=160)
    program: str | None = Field(default=None, max_length=160)
    year_of_study: int | None = Field(default=None, ge=1, le=8)
    semester: int | None = Field(default=None, ge=1, le=3)
    avatar_path: str | None = Field(default=None, max_length=512)


class DeleteAccountResponse(BaseModel):
    deleted: bool = True
    message: str
