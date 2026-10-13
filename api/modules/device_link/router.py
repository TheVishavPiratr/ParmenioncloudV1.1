"""HTTP endpoints for the device_link module.

Logging in, refreshing and logging out use the refresh token; managing devices
needs a normal login token (CurrentUser).
"""

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy.ext.asyncio import AsyncSession

from api.core.database import get_db
from api.modules.auth.dependencies import CurrentUser
from api.modules.device_link import service
from api.modules.device_link.models import LinkedDevice
from api.modules.device_link.schemas import (
    DeviceLoginRequest,
    DeviceResponse,
    DeviceUpdateRequest,
    LogoutAllResponse,
    PushTokenRequest,
    RefreshRequest,
    TokenPairResponse,
)

router = APIRouter(prefix="/devices", tags=["devices"])

DbSession = Annotated[AsyncSession, Depends(get_db)]

_bearer_scheme = HTTPBearer(auto_error=False)

_ERRORS: dict[type[service.DeviceError], tuple[int, str]] = {
    # The same wording and codes as POST /auth/login.
    service.InvalidCredentialsError: (
        status.HTTP_401_UNAUTHORIZED,
        "Incorrect email or password",
    ),
    service.InactiveUserError: (status.HTTP_403_FORBIDDEN, "Account is disabled"),
    service.EmailNotVerifiedError: (
        status.HTTP_403_FORBIDDEN,
        "Email is not verified",
    ),
    service.InvalidRefreshTokenError: (
        status.HTTP_401_UNAUTHORIZED,
        "Session expired; log in again",
    ),
    service.DeviceNotFoundError: (status.HTTP_404_NOT_FOUND, "Device not found"),
    service.PushTokenConflictError: (
        status.HTTP_409_CONFLICT,
        "Push token was claimed at the same time; send it again",
    ),
}


def _http_error(exc: service.DeviceError) -> HTTPException:
    code, detail = _ERRORS[type(exc)]
    return HTTPException(status_code=code, detail=detail)


async def _current_device_id(
    credentials: Annotated[
        HTTPAuthorizationCredentials | None, Depends(_bearer_scheme)
    ],
) -> uuid.UUID | None:
    """Which linked device the request's login token belongs to, if any."""
    if credentials is None:
        return None
    return service.device_id_from_token(credentials.credentials)


CurrentDeviceId = Annotated[uuid.UUID | None, Depends(_current_device_id)]


def _client_ip(request: Request) -> str | None:
    return request.client.host if request.client else None


def _to_response(device: LinkedDevice, current_id: uuid.UUID | None) -> DeviceResponse:
    return DeviceResponse(
        id=device.id,
        name=device.name,
        platform=device.platform,
        app_version=device.app_version,
        os_version=device.os_version,
        current=device.id == current_id,
        notifications_enabled=device.notifications_enabled,
        has_push_token=device.push_token is not None,
        last_seen_at=device.last_seen_at,
        last_ip=device.last_ip,
        created_at=device.created_at,
    )


# ---------- sessions ----------


@router.post("/login")
async def login(
    data: DeviceLoginRequest, request: Request, db: DbSession
) -> TokenPairResponse:
    """Log in from the app and link this device. Keep the refresh token in the
    device's secure storage; it is shown only once."""
    try:
        return await service.login(db, data, _client_ip(request))
    except service.DeviceError as exc:
        raise _http_error(exc) from None


@router.post("/refresh")
async def refresh(
    data: RefreshRequest, request: Request, db: DbSession
) -> TokenPairResponse:
    """New access token and new refresh token; the old refresh token stops
    working. Never send the same refresh token twice."""
    try:
        return await service.refresh(db, data.refresh_token, _client_ip(request))
    except service.DeviceError as exc:
        raise _http_error(exc) from None


@router.post("/logout", status_code=status.HTTP_204_NO_CONTENT)
async def logout(data: RefreshRequest, db: DbSession) -> Response:
    await service.logout(db, data.refresh_token)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post("/logout-all")
async def logout_all(
    user: CurrentUser, current: CurrentDeviceId, db: DbSession
) -> LogoutAllResponse:
    """Log out every other device. The device asking stays logged in."""
    count = await service.logout_all(db, user.id, current)
    return LogoutAllResponse(logged_out=count)


# ---------- devices ----------


@router.get("")
async def list_devices(
    user: CurrentUser, current: CurrentDeviceId, db: DbSession
) -> list[DeviceResponse]:
    return [_to_response(d, current) for d in await service.list_devices(db, user.id)]


@router.patch("/{device_id}")
async def update_device(
    device_id: uuid.UUID,
    data: DeviceUpdateRequest,
    user: CurrentUser,
    current: CurrentDeviceId,
    db: DbSession,
) -> DeviceResponse:
    try:
        device = await service.update_device(db, user.id, device_id, data)
    except service.DeviceError as exc:
        raise _http_error(exc) from None
    return _to_response(device, current)


@router.delete("/{device_id}", status_code=status.HTTP_204_NO_CONTENT)
async def remove_device(
    device_id: uuid.UUID, user: CurrentUser, db: DbSession
) -> Response:
    """Log a device out remotely, e.g. a lost phone."""
    try:
        await service.remove_device(db, user.id, device_id)
    except service.DeviceError as exc:
        raise _http_error(exc) from None
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.put("/{device_id}/push-token")
async def set_push_token(
    device_id: uuid.UUID,
    data: PushTokenRequest,
    user: CurrentUser,
    current: CurrentDeviceId,
    db: DbSession,
) -> DeviceResponse:
    """Register the token from Firebase so this device gets alerts."""
    try:
        device = await service.set_push_token(
            db, user.id, device_id, data.provider, data.token
        )
    except service.DeviceError as exc:
        raise _http_error(exc) from None
    return _to_response(device, current)


@router.delete("/{device_id}/push-token")
async def clear_push_token(
    device_id: uuid.UUID, user: CurrentUser, current: CurrentDeviceId, db: DbSession
) -> DeviceResponse:
    try:
        device = await service.clear_push_token(db, user.id, device_id)
    except service.DeviceError as exc:
        raise _http_error(exc) from None
    return _to_response(device, current)
