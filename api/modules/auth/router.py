"""HTTP endpoints for the auth module."""

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession

from api.core.database import get_db
from api.modules.auth import service
from api.modules.auth.dependencies import CurrentUser
from api.modules.auth.schemas import (
    EmailRequest,
    LoginRequest,
    MessageResponse,
    ResetPasswordRequest,
    SignupRequest,
    TokenResponse,
    UserResponse,
    VerifyEmailRequest,
)

router = APIRouter(prefix="/auth", tags=["auth"])

DbSession = Annotated[AsyncSession, Depends(get_db)]

_INVALID_CODE = HTTPException(
    status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid or expired code"
)


@router.post("/signup", status_code=status.HTTP_201_CREATED)
async def signup(data: SignupRequest, db: DbSession) -> UserResponse:
    """Create an account and email a verification code."""
    try:
        user = await service.register_user(db, data)
    except service.EmailAlreadyRegisteredError:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT, detail="Email is already registered"
        ) from None
    return UserResponse.model_validate(user)


@router.post("/verify-email")
async def verify_email(data: VerifyEmailRequest, db: DbSession) -> MessageResponse:
    try:
        await service.verify_email(db, data.email, data.code)
    except service.InvalidOtpError:
        raise _INVALID_CODE from None
    return MessageResponse(message="Email verified. You can now log in.")


@router.post("/resend-verification", status_code=status.HTTP_202_ACCEPTED)
async def resend_verification(data: EmailRequest, db: DbSession) -> MessageResponse:
    # Same reply whether or not the account exists, so emails can't be probed.
    await service.resend_verification(db, data.email)
    return MessageResponse(
        message="If that account needs verification, a new code has been sent."
    )


@router.post("/forgot-password", status_code=status.HTTP_202_ACCEPTED)
async def forgot_password(data: EmailRequest, db: DbSession) -> MessageResponse:
    # Same reply whether or not the account exists, so emails can't be probed.
    await service.request_password_reset(db, data.email)
    return MessageResponse(
        message="If that email is registered, a reset code has been sent."
    )


@router.post("/reset-password")
async def reset_password(data: ResetPasswordRequest, db: DbSession) -> MessageResponse:
    try:
        await service.reset_password(db, data.email, data.code, data.new_password)
    except service.InvalidOtpError:
        raise _INVALID_CODE from None
    return MessageResponse(message="Password updated. You can now log in.")


@router.post("/login")
async def login(data: LoginRequest, db: DbSession) -> TokenResponse:
    try:
        user = await service.authenticate(db, data.email, data.password)
    except service.InvalidCredentialsError:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Incorrect email or password",
        ) from None
    except service.InactiveUserError:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN, detail="Account is disabled"
        ) from None
    except service.EmailNotVerifiedError:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN, detail="Email is not verified"
        ) from None
    return TokenResponse(
        access_token=service.create_access_token(
            user.id, session_version=user.session_version
        )
    )


@router.get("/me")
async def me(current_user: CurrentUser) -> UserResponse:
    return UserResponse.model_validate(current_user)
