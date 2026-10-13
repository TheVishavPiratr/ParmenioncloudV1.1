"""Pydantic request/response schemas for the device_link module."""

import uuid
from datetime import datetime
from typing import Annotated, Self

from pydantic import (
    BaseModel,
    EmailStr,
    Field,
    StringConstraints,
    field_validator,
    model_validator,
)

from api.modules.device_link.models import Platform, PushProvider

# Single-line text: no control characters. PostgreSQL also rejects NUL (\x00)
# in text columns outright.
_NO_CONTROL_CHARS = r"^[^\x00-\x1f\x7f]*$"

DeviceName = Annotated[
    str,
    StringConstraints(
        strip_whitespace=True, min_length=1, max_length=100, pattern=_NO_CONTROL_CHARS
    ),
]
ShortText = Annotated[
    str, StringConstraints(min_length=1, max_length=64, pattern=_NO_CONTROL_CHARS)
]
RefreshToken = Annotated[
    str, StringConstraints(min_length=1, max_length=200, pattern=_NO_CONTROL_CHARS)
]
# FCM registration tokens use letters, digits and ":_-".
PushToken = Annotated[
    str, StringConstraints(min_length=1, max_length=512, pattern=r"^[A-Za-z0-9:_-]+$")
]


class DeviceInfo(BaseModel):
    name: DeviceName
    platform: Platform
    app_version: ShortText | None = None
    os_version: ShortText | None = None


class DeviceLoginRequest(BaseModel):
    email: EmailStr
    password: str = Field(min_length=1, max_length=128)
    device: DeviceInfo

    @field_validator("email")
    @classmethod
    def lowercase_email(cls, value: str) -> str:
        return value.lower()


class RefreshRequest(BaseModel):
    refresh_token: RefreshToken


class TokenPairResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"
    # Shown only here; send it to /devices/refresh before the access token ends.
    refresh_token: str
    refresh_expires_at: datetime
    device_id: uuid.UUID


class DeviceUpdateRequest(BaseModel):
    """Only the fields sent are changed."""

    name: DeviceName | None = None
    notifications_enabled: bool | None = None

    @model_validator(mode="after")
    def _fields_not_null(self) -> Self:
        for field in self.model_fields_set:
            if getattr(self, field) is None:
                raise ValueError(f"{field} cannot be null")
        return self


class PushTokenRequest(BaseModel):
    provider: PushProvider
    token: PushToken


class DeviceResponse(BaseModel):
    id: uuid.UUID
    name: str
    platform: str
    app_version: str | None
    os_version: str | None
    # True for the device making this request.
    current: bool
    notifications_enabled: bool
    has_push_token: bool
    last_seen_at: datetime
    last_ip: str | None
    created_at: datetime


class LogoutAllResponse(BaseModel):
    logged_out: int


class PushTarget(BaseModel):
    """Where to send a push alert; used by the notifications module."""

    device_id: uuid.UUID
    provider: str
    token: str
