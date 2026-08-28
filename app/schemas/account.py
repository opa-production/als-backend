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

    # `avatar_path` is deliberately not here. It named an object in a private
    # bucket, so a client that could set it freely could point it at somebody
    # else's file and then read that file back through /me/avatar-url. It is
    # set only by POST /me/avatar, which checks the path against the caller.


class DeleteAccountResponse(BaseModel):
    deleted: bool = True
    message: str


class AvatarUploadUrlRequest(BaseModel):
    """What the device is about to send, so it can be refused before it does."""

    mime_type: str = Field(max_length=128)
    byte_size: int = Field(gt=0)


class AvatarUploadUrlResponse(BaseModel):
    upload_url: str
    bucket: str
    #: Hand this back to POST /me/avatar once the upload has landed.
    path: str
    token: str


class ConfirmAvatarRequest(BaseModel):
    path: str = Field(max_length=512)


class AvatarUrlResponse(BaseModel):
    url: str
    expires_in: int
