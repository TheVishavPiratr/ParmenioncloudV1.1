"""Business logic for the device_link module.

A linked device is a lasting login: a 30-minute access token plus a refresh
token that is swapped for a new one on every use. Using a swapped-out refresh
token again means it was copied, so the device is logged out at once.

Other modules may call: list_push_targets, drop_push_token.
"""

import hashlib
import secrets
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from api.modules.audit import service as audit_service
from api.modules.auth import service as auth_service
from api.modules.device_link import repository
from api.modules.device_link.models import LinkedDevice, PushProvider
from api.modules.device_link.schemas import (
    DeviceLoginRequest,
    DeviceUpdateRequest,
    PushTarget,
    TokenPairResponse,
)
from api.modules.notifications import service as notifications_service

AuditAction = audit_service.AuditAction
AuditActor = audit_service.AuditActor

REFRESH_TOKEN_PREFIX = "vpr_"
# Unused for this long, a device has to log in again.
REFRESH_TTL = timedelta(days=60)
# Linking one more logs out the device unused the longest.
MAX_DEVICES_PER_USER = 20


class DeviceError(Exception):
    """Base of every error this service raises."""


class InvalidCredentialsError(DeviceError):
    pass


class InactiveUserError(DeviceError):
    pass


class EmailNotVerifiedError(DeviceError):
    pass


class InvalidRefreshTokenError(DeviceError):
    """Unknown, expired, reused, or ended by a password reset: one answer for all."""


class DeviceNotFoundError(DeviceError):
    pass


class PushTokenConflictError(DeviceError):
    """Another device claimed the same push token at the same moment."""


def _utcnow() -> datetime:
    return datetime.now(UTC)


def _as_utc(value: datetime) -> datetime:
    # Some databases return naive datetimes; every stored time is UTC.
    return value if value.tzinfo else value.replace(tzinfo=UTC)


def _new_refresh_token() -> str:
    return REFRESH_TOKEN_PREFIX + secrets.token_urlsafe(32)


def _hash_token(token: str) -> str:
    # 256-bit random tokens: a fast unsalted hash is enough, as for agent tokens.
    return hashlib.sha256(token.encode()).hexdigest()


def _token_pair(device: LinkedDevice, refresh_token: str) -> TokenPairResponse:
    return TokenPairResponse(
        access_token=auth_service.create_access_token(
            device.user_id,
            device_id=device.id,
            session_version=device.session_version,
        ),
        refresh_token=refresh_token,
        refresh_expires_at=device.refresh_expires_at,
        device_id=device.id,
    )


async def _audit_device(
    db: AsyncSession,
    action: AuditAction,
    user_id: uuid.UUID,
    device_id: uuid.UUID,
    *,
    success: bool = True,
    actor: AuditActor | None = None,
    details: dict[str, Any] | None = None,
) -> None:
    await audit_service.record(
        db,
        action,
        user_id=user_id,
        success=success,
        actor=actor,
        target_type="device",
        target_id=device_id,
        details=details,
    )


def device_id_from_token(token: str) -> uuid.UUID | None:
    """The linked device a login token belongs to, if any."""
    try:
        return auth_service.get_token_claims(token).device_id
    except auth_service.InvalidTokenError:
        return None


# ---------- logging in and staying logged in ----------


async def login(
    db: AsyncSession, data: DeviceLoginRequest, ip: str | None
) -> TokenPairResponse:
    """Check the password and link this device to the account."""
    try:
        user = await auth_service.authenticate(db, data.email, data.password)
    except auth_service.InvalidCredentialsError:
        raise InvalidCredentialsError from None
    except auth_service.InactiveUserError:
        raise InactiveUserError from None
    except auth_service.EmailNotVerifiedError:
        raise EmailNotVerifiedError from None

    over = await repository.count_devices(db, user.id) - MAX_DEVICES_PER_USER + 1
    if over > 0:
        evicted = await repository.least_recently_used(db, user.id, over)
        await repository.delete_devices(db, evicted)
        for device_id in evicted:
            await _audit_device(
                db,
                AuditAction.DEVICE_EVICTED,
                user.id,
                device_id,
                details={"reason": "device_limit"},
            )

    now = _utcnow()
    refresh_token = _new_refresh_token()
    device = LinkedDevice(
        id=uuid.uuid4(),
        user_id=user.id,
        name=data.device.name,
        platform=data.device.platform,
        app_version=data.device.app_version,
        os_version=data.device.os_version,
        refresh_token_hash=_hash_token(refresh_token),
        refresh_expires_at=now + REFRESH_TTL,
        session_version=user.session_version,
        notifications_enabled=True,
        last_seen_at=now,
        last_ip=ip,
        created_at=now,
    )
    await repository.add_device(db, device)
    await _audit_device(
        db,
        AuditAction.DEVICE_LINKED,
        user.id,
        device.id,
        details={"name": device.name, "platform": device.platform},
    )
    await notifications_service.notify(
        db,
        user.id,
        notifications_service.NotificationCategory.SECURITY,
        "device_linked",
        "New sign-in",
        f"{device.name} ({device.platform}) signed in to your account. If this "
        f"wasn't you, sign it out under Devices and change your password.",
        {"device_id": str(device.id)},
    )
    return _token_pair(device, refresh_token)


async def refresh(
    db: AsyncSession, refresh_token: str, ip: str | None
) -> TokenPairResponse:
    """Swap a refresh token for a new access token and a new refresh token."""
    if not refresh_token.startswith(REFRESH_TOKEN_PREFIX):
        raise InvalidRefreshTokenError
    token_hash = _hash_token(refresh_token)
    device = await repository.get_by_refresh_hash(db, token_hash)
    if device is None:
        copied = await repository.get_by_previous_hash(db, token_hash)
        if copied is not None:
            # A swapped-out token came back: someone holds a copy. Log the
            # device out; its owner just logs in again.
            user_id, device_id, name = copied.user_id, copied.id, copied.name
            await repository.delete_devices(db, [device_id])
            await _audit_device(
                db,
                AuditAction.REFRESH_TOKEN_REUSED,
                user_id,
                device_id,
                success=False,
                actor=AuditActor.ANONYMOUS,
            )
            await notifications_service.notify(
                db,
                user_id,
                notifications_service.NotificationCategory.SECURITY,
                "session_reused",
                "Sign-in used from two places",
                f"{name} was signed out because its sign-in was used from "
                f"somewhere else. Sign in on it again. If this keeps happening, "
                f"change your password.",
                {"device_id": str(device_id)},
            )
        raise InvalidRefreshTokenError

    now = _utcnow()
    expired = _as_utc(device.refresh_expires_at) <= now
    if expired or not await auth_service.is_session_current(
        db, device.user_id, device.session_version
    ):
        await repository.delete_devices(db, [device.id])
        raise InvalidRefreshTokenError

    new_token = _new_refresh_token()
    swapped = await repository.rotate_refresh_token(
        db,
        device.id,
        token_hash,
        {
            "refresh_token_hash": _hash_token(new_token),
            "refresh_expires_at": now + REFRESH_TTL,
            "last_seen_at": now,
            "last_ip": ip,
        },
    )
    await repository.commit(db)
    if not swapped:
        # Two refreshes with the same token at once: only the first one wins.
        raise InvalidRefreshTokenError
    return _token_pair(device, new_token)


async def logout(db: AsyncSession, refresh_token: str) -> None:
    """Unlink the device holding this token. Unknown tokens are ignored, so the
    answer never tells whether a token was valid."""
    device = await repository.get_by_refresh_hash(db, _hash_token(refresh_token))
    if device is not None:
        user_id, device_id = device.user_id, device.id
        await repository.delete_devices(db, [device_id])
        await _audit_device(db, AuditAction.DEVICE_LOGGED_OUT, user_id, device_id)


# ---------- managing devices ----------


async def list_devices(db: AsyncSession, user_id: uuid.UUID) -> list[LinkedDevice]:
    return await repository.list_devices(db, user_id)


async def get_device(
    db: AsyncSession, user_id: uuid.UUID, device_id: uuid.UUID
) -> LinkedDevice:
    device = await repository.get_device(db, user_id, device_id)
    if device is None:
        raise DeviceNotFoundError
    return device


async def update_device(
    db: AsyncSession,
    user_id: uuid.UUID,
    device_id: uuid.UUID,
    data: DeviceUpdateRequest,
) -> LinkedDevice:
    device = await get_device(db, user_id, device_id)
    changes = data.model_dump(exclude_unset=True)
    for field, value in changes.items():
        setattr(device, field, value)
    await repository.commit(db)
    await _audit_device(
        db,
        AuditAction.DEVICE_UPDATED,
        user_id,
        device.id,
        details={"fields": sorted(changes)},
    )
    return device


async def remove_device(
    db: AsyncSession, user_id: uuid.UUID, device_id: uuid.UUID
) -> None:
    """Log a device out remotely, e.g. a lost phone."""
    device = await get_device(db, user_id, device_id)
    await repository.delete_devices(db, [device.id])
    await _audit_device(db, AuditAction.DEVICE_REMOVED, user_id, device_id)


async def logout_all(
    db: AsyncSession, user_id: uuid.UUID, keep_device_id: uuid.UUID | None
) -> int:
    """Log out every device except the one asking. Returns how many."""
    count = await repository.delete_other_devices(db, user_id, keep_device_id)
    await audit_service.record(
        db,
        AuditAction.DEVICES_LOGGED_OUT_ALL,
        user_id=user_id,
        details={"count": count},
    )
    return count


# ---------- push tokens ----------


async def set_push_token(
    db: AsyncSession,
    user_id: uuid.UUID,
    device_id: uuid.UUID,
    provider: PushProvider,
    token: str,
) -> LinkedDevice:
    device = await get_device(db, user_id, device_id)
    # The phone may have been logged in to another account before.
    await repository.release_push_token(db, provider, token, except_id=device.id)
    device.push_provider = provider
    device.push_token = token
    try:
        await repository.commit(db)
    except IntegrityError:
        raise PushTokenConflictError from None
    # The provider only: the token itself lets anyone push to the phone.
    await _audit_device(
        db,
        AuditAction.PUSH_TOKEN_SET,
        user_id,
        device.id,
        details={"provider": provider},
    )
    return device


async def clear_push_token(
    db: AsyncSession, user_id: uuid.UUID, device_id: uuid.UUID
) -> LinkedDevice:
    device = await get_device(db, user_id, device_id)
    device.push_provider = None
    device.push_token = None
    await repository.commit(db)
    await _audit_device(db, AuditAction.PUSH_TOKEN_CLEARED, user_id, device.id)
    return device


async def list_push_targets(db: AsyncSession, user_id: uuid.UUID) -> list[PushTarget]:
    """Devices to send a push alert to: linked, with a token, alerts switched on."""
    return [
        PushTarget(device_id=d.id, provider=d.push_provider, token=d.push_token)
        for d in await repository.list_push_targets(db, user_id)
    ]


async def drop_push_token(db: AsyncSession, provider: str, token: str) -> None:
    """Forget a token the push service reported as no longer valid."""
    await repository.release_push_token(db, provider, token)
    await repository.commit(db)
