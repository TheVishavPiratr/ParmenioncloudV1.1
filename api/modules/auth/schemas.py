"""Pydantic request/response schemas for the auth module."""

import uuid
from datetime import datetime
from typing import Annotated

from pydantic import (
    BaseModel,
    ConfigDict,
    EmailStr,
    Field,
    StringConstraints,
    field_validator,
)

Password = Annotated[str, Field(min_length=8, max_length=128)]
OtpCodeStr = Annotated[str, Field(pattern=r"^\d{6}$")]


class EmailRequest(BaseModel):
    email: EmailStr

    @field_validator("email")
    @classmethod
    def lowercase_email(cls, value: str) -> str:
        return value.lower()


class SignupRequest(EmailRequest):
    password: Password
    full_name: Annotated[
        str, StringConstraints(strip_whitespace=True, min_length=1, max_length=100)
    ]


class LoginRequest(EmailRequest):
    password: str = Field(min_length=1, max_length=128)


class VerifyEmailRequest(EmailRequest):
    code: OtpCodeStr


class ResetPasswordRequest(EmailRequest):
    code: OtpCodeStr
    new_password: Password


class UserResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    email: str
    full_name: str
    is_active: bool
    is_verified: bool
    created_at: datetime


class TokenResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"


class MessageResponse(BaseModel):
    message: str
