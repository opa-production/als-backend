import uuid

from pydantic import BaseModel, ConfigDict, Field


class OtpRequest(BaseModel):
    phone: str = Field(
        examples=["+254712345678"],
        description="Full E.164, including the country code.",
    )


class OtpRequestResponse(BaseModel):
    """
    Deliberately says nothing about whether the number has an account.

    A response that differed would turn this endpoint into a way to test which
    numbers are registered, one request at a time.
    """

    sent: bool = True
    expires_in_seconds: int

    #: Only ever populated when no SMS provider is configured, so the flow can
    #: be tested through Swagger. Absent the moment credentials exist.
    debug_code: str | None = Field(
        default=None,
        description="Development only. Never present when SMS is configured.",
    )


class OtpVerifyRequest(BaseModel):
    phone: str = Field(examples=["+254712345678"])
    code: str = Field(min_length=4, max_length=8, examples=["123456"])

    #: The client mints this. Sending the same id after a reinstall updates one
    #: device row rather than growing a new one every launch.
    device_id: uuid.UUID | None = None
    platform: str = Field(default="", max_length=16, examples=["android"])
    app_version: str = Field(default="", max_length=32, examples=["1.0.0"])


class GoogleSignInRequest(BaseModel):
    id_token: str = Field(description="The ID token from Google Sign-In on the device.")
    device_id: uuid.UUID | None = None
    platform: str = Field(default="", max_length=16)
    app_version: str = Field(default="", max_length=32)


class RefreshRequest(BaseModel):
    refresh_token: str


class LogoutRequest(BaseModel):
    #: Omit to sign out everywhere — which is what a lost phone needs.
    device_id: uuid.UUID | None = None


class TokenPair(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    access_token: str
    refresh_token: str
    token_type: str = "bearer"
    expires_in: int = Field(description="Access token lifetime, in seconds.")

    user_id: uuid.UUID
    #: True when this request created the account, so the app knows to send a
    #: new student to onboarding rather than straight to the tabs.
    is_new_user: bool = False
