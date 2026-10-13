"""Business logic for the auth module.

Other modules may call: authenticate, create_access_token, get_email,
get_token_claims, is_active_user, is_session_current.
"""

import asyncio
import hashlib
import hmac
import secrets
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

import jwt
from pwdlib import PasswordHash
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from api.core.config import settings
from api.modules.audit import service as audit_service
from api.modules.auth import repository
from api.modules.auth.models import OtpCode, OtpPurpose, User
from api.modules.auth.schemas import SignupRequest
from api.modules.notifications import service as notifications_service

AuditAction = audit_service.AuditAction
AuditActor = audit_service.AuditActor

OTP_LENGTH = 6
OTP_TTL = timedelta(minutes=10)
OTP_MAX_ATTEMPTS = 5
OTP_RESEND_COOLDOWN = timedelta(seconds=60)

_password_hash = PasswordHash.recommended()
# Checked against when the email is unknown, so a login attempt takes the same
# time whether or not the account exists.
_DUMMY_HASH = _password_hash.hash("dummy-password-for-timing")


class EmailAlreadyRegisteredError(Exception):
    pass


class InvalidCredentialsError(Exception):
    pass


class InactiveUserError(Exception):
    pass


class EmailNotVerifiedError(Exception):
    pass


class InvalidTokenError(Exception):
    pass


class InvalidOtpError(Exception):
    pass


@dataclass(frozen=True)
class TokenClaims:
    """What a valid login token says about its holder."""

    user_id: uuid.UUID
    # Set when the token was issued to a linked device (device_link module).
    device_id: uuid.UUID | None
    session_version: int
    issued_at: datetime


def _utcnow() -> datetime:
    return datetime.now(UTC)


def _as_utc(value: datetime) -> datetime:
    # Some databases return naive datetimes; every stored time is UTC.
    return value if value.tzinfo else value.replace(tzinfo=UTC)


async def _hash_password(password: str) -> str:
    # Argon2 is deliberately slow; keep it off the event loop.
    return await asyncio.to_thread(_password_hash.hash, password)


async def _verify_password(password: str, hashed: str) -> bool:
    return await asyncio.to_thread(_password_hash.verify, password, hashed)


def _hash_otp(otp_id: uuid.UUID, code: str) -> str:
    # Keyed with the server secret, so a leaked table can't be brute-forced offline.
    message = f"{otp_id}:{code}".encode()
    return hmac.new(settings.jwt_secret.encode(), message, hashlib.sha256).hexdigest()


async def _issue_otp(db: AsyncSession, user: User, purpose: OtpPurpose) -> None:
    """Create a fresh code for the user and email it.

    Silently does nothing if the previous code was sent less than a minute ago.
    """
    now = _utcnow()
    current = await repository.get_active_otp(db, user.id, purpose)
    if current and now - _as_utc(current.created_at) < OTP_RESEND_COOLDOWN:
        return

    code = f"{secrets.randbelow(10**OTP_LENGTH):0{OTP_LENGTH}d}"
    otp_id = uuid.uuid4()
    await repository.replace_otp(
        db,
        OtpCode(
            id=otp_id,
            user_id=user.id,
            purpose=purpose,
            code_hash=_hash_otp(otp_id, code),
            attempts=0,
            expires_at=now + OTP_TTL,
            created_at=now,
        ),
    )

    minutes = int(OTP_TTL.total_seconds() // 60)
    if purpose is OtpPurpose.EMAIL_VERIFICATION:
        subject = "Verify your VisionPulse email"
        action = "verify your email address"
    else:
        subject = "Reset your VisionPulse password"
        action = "reset your password"
    await notifications_service.send_email(
        to=user.email,
        subject=subject,
        body=(
            f"Hi {user.full_name},\n\n"
            f"Your code to {action} is: {code}\n\n"
            f"It expires in {minutes} minutes. If you didn't request this, "
            f"you can ignore this email."
        ),
    )


async def _use_otp(
    db: AsyncSession, user: User, purpose: OtpPurpose, code: str
) -> None:
    """Check a code and mark it used (uncommitted). Raises InvalidOtpError."""
    now = _utcnow()
    otp = await repository.get_active_otp(db, user.id, purpose)
    if (
        otp is None
        or _as_utc(otp.expires_at) <= now
        or otp.attempts >= OTP_MAX_ATTEMPTS
    ):
        raise InvalidOtpError
    if not hmac.compare_digest(otp.code_hash, _hash_otp(otp.id, code)):
        await repository.record_failed_otp_attempt(db, otp.id)
        raise InvalidOtpError
    if not await repository.consume_otp(db, otp.id, now):
        raise InvalidOtpError


async def register_user(db: AsyncSession, data: SignupRequest) -> User:
    if await repository.get_user_by_email(db, data.email) is not None:
        raise EmailAlreadyRegisteredError
    user = User(
        email=data.email,
        password_hash=await _hash_password(data.password),
        full_name=data.full_name,
        is_active=True,
        is_verified=False,
    )
    try:
        await repository.add_user(db, user)
    except IntegrityError as exc:
        # Another request registered the same email between the check and the insert.
        raise EmailAlreadyRegisteredError from exc
    await audit_service.record(db, AuditAction.SIGNUP, user_id=user.id)
    await _issue_otp(db, user, OtpPurpose.EMAIL_VERIFICATION)
    return user


async def verify_email(db: AsyncSession, email: str, code: str) -> None:
    user = await repository.get_user_by_email(db, email)
    if user is None or user.is_verified:
        raise InvalidOtpError
    await _use_otp(db, user, OtpPurpose.EMAIL_VERIFICATION, code)
    user.is_verified = True
    await repository.commit(db)
    await audit_service.record(db, AuditAction.EMAIL_VERIFIED, user_id=user.id)


async def resend_verification(db: AsyncSession, email: str) -> None:
    """Send a new verification code. Does nothing for unknown or verified emails."""
    user = await repository.get_user_by_email(db, email)
    if user is not None and user.is_active and not user.is_verified:
        await _issue_otp(db, user, OtpPurpose.EMAIL_VERIFICATION)


async def request_password_reset(db: AsyncSession, email: str) -> None:
    """Send a password reset code. Does nothing for unknown or disabled accounts."""
    user = await repository.get_user_by_email(db, email)
    if user is not None and user.is_active:
        await _issue_otp(db, user, OtpPurpose.PASSWORD_RESET)
        # Anyone who knows the email can ask, so this is not the owner acting.
        await audit_service.record(
            db,
            AuditAction.PASSWORD_RESET_REQUESTED,
            user_id=user.id,
            actor=AuditActor.ANONYMOUS,
        )


async def reset_password(
    db: AsyncSession, email: str, code: str, new_password: str
) -> None:
    user = await repository.get_user_by_email(db, email)
    if user is None or not user.is_active:
        raise InvalidOtpError
    await _use_otp(db, user, OtpPurpose.PASSWORD_RESET, code)
    user.password_hash = await _hash_password(new_password)
    # The code arrived by email, so the user has proven they own the address.
    user.is_verified = True
    # Log out everywhere: older tokens and linked devices stop working.
    user.session_version += 1
    await repository.commit(db)
    await audit_service.record(db, AuditAction.PASSWORD_RESET, user_id=user.id)
    await notifications_service.notify(
        db,
        user.id,
        notifications_service.NotificationCategory.SECURITY,
        "password_changed",
        "Password changed",
        "Your password was changed and every device was signed out. If this "
        "wasn't you, reset your password now and check your linked devices.",
    )


def _email_fingerprint(email: str) -> str:
    """A keyed hash of an email, so repeated attempts on an address that has no
    account can be grouped in the audit log without storing the address."""
    message = f"audit-email:{email.strip().lower()}".encode()
    return hmac.new(settings.jwt_secret.encode(), message, hashlib.sha256).hexdigest()


async def _login_failed(
    db: AsyncSession, user_id: uuid.UUID | None, reason: str, email: str
) -> None:
    details = {"reason": reason}
    if user_id is None:
        # Nobody owns the entry, so nobody sees it through the API.
        details["email_fingerprint"] = _email_fingerprint(email)
    await audit_service.record(
        db,
        AuditAction.LOGIN_FAILED,
        user_id=user_id,
        success=False,
        actor=AuditActor.ANONYMOUS,
        details=details,
    )


async def authenticate(db: AsyncSession, email: str, password: str) -> User:
    """Check a password login. Every attempt, failed or not, is audited."""
    user = await repository.get_user_by_email(db, email)
    if user is None:
        await _verify_password(password, _DUMMY_HASH)
        await _login_failed(db, None, "unknown_email", email)
        raise InvalidCredentialsError
    if not await _verify_password(password, user.password_hash):
        await _login_failed(db, user.id, "wrong_password", email)
        raise InvalidCredentialsError
    if not user.is_active:
        await _login_failed(db, user.id, "account_disabled", email)
        raise InactiveUserError
    if not user.is_verified:
        await _login_failed(db, user.id, "email_not_verified", email)
        raise EmailNotVerifiedError
    await audit_service.record(db, AuditAction.LOGIN_SUCCEEDED, user_id=user.id)
    return user


async def confirm_password(user: User, password: str) -> bool:
    """Re-check the password of a logged-in user before a drastic action."""
    return await _verify_password(password, user.password_hash)


async def delete_user(db: AsyncSession, user_id: uuid.UUID) -> None:
    """Delete the account row. Every table keyed to it cascades; files and
    live connections must be cleaned up by their modules first."""
    await repository.delete_user(db, user_id)


def create_access_token(
    user_id: uuid.UUID,
    *,
    device_id: uuid.UUID | None = None,
    session_version: int = 0,
) -> str:
    """A short-lived login token. Pass the user's current session_version, so a
    later password reset can end it; device_id marks a linked device's token."""
    now = _utcnow()
    payload = {
        "sub": str(user_id),
        "iat": now,
        "exp": now + timedelta(minutes=settings.access_token_expire_minutes),
        "sv": session_version,
    }
    if device_id is not None:
        payload["did"] = str(device_id)
    return jwt.encode(payload, settings.jwt_secret, algorithm=settings.jwt_algorithm)


async def get_email(db: AsyncSession, user_id: uuid.UUID) -> str | None:
    """Where to email the user, or None if the account is gone or disabled."""
    user = await repository.get_user_by_id(db, user_id)
    return user.email if user is not None and user.is_active else None


async def is_active_user(db: AsyncSession, user_id: uuid.UUID) -> bool:
    """Whether the account exists and is not disabled. For other modules whose
    records (e.g. edge agents) must stop working when their owner is disabled."""
    user = await repository.get_user_by_id(db, user_id)
    return user is not None and user.is_active


async def is_session_current(
    db: AsyncSession, user_id: uuid.UUID, session_version: int
) -> bool:
    """Whether the account is active and no password reset happened since a
    session was started under `session_version`."""
    user = await repository.get_user_by_id(db, user_id)
    return (
        user is not None and user.is_active and user.session_version == session_version
    )


def get_token_claims(token: str) -> TokenClaims:
    """Check a token's signature and expiry and return what it says.
    Raises InvalidTokenError. Does not look the user up."""
    try:
        payload = jwt.decode(
            token,
            settings.jwt_secret,
            algorithms=[settings.jwt_algorithm],
            options={"require": ["exp", "sub", "iat"]},
        )
        device = payload.get("did")
        session_version = payload.get("sv", 0)
        if not isinstance(session_version, int) or isinstance(session_version, bool):
            raise TypeError("sv must be an integer")
        return TokenClaims(
            user_id=uuid.UUID(payload["sub"]),
            device_id=uuid.UUID(device) if device is not None else None,
            session_version=session_version,
            issued_at=datetime.fromtimestamp(payload["iat"], UTC),
        )
    except (jwt.PyJWTError, ValueError, TypeError) as exc:
        raise InvalidTokenError from exc


async def get_user_from_token(db: AsyncSession, token: str) -> User:
    claims = get_token_claims(token)
    user = await repository.get_user_by_id(db, claims.user_id)
    if (
        user is None
        or not user.is_active
        or user.session_version != claims.session_version
    ):
        raise InvalidTokenError
    return user
