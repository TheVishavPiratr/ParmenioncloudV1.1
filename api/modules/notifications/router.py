"""HTTP endpoints for the notifications module: the in-app inbox, alert
settings per category, and a test alert."""

import uuid
from datetime import datetime
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, Response, status
from sqlalchemy.ext.asyncio import AsyncSession

from api.core.database import get_db
from api.modules.auth.dependencies import CurrentUser
from api.modules.notifications import service
from api.modules.notifications.models import Notification, NotificationCategory
from api.modules.notifications.schemas import (
    MarkedReadResponse,
    NotificationResponse,
    PreferenceResponse,
    PreferenceUpdateRequest,
    TestPushResponse,
    UnreadCountResponse,
)

router = APIRouter(prefix="/notifications", tags=["notifications"])

DbSession = Annotated[AsyncSession, Depends(get_db)]

_NOT_FOUND = HTTPException(
    status_code=status.HTTP_404_NOT_FOUND, detail="Notification not found"
)


def _to_response(notification: Notification) -> NotificationResponse:
    return NotificationResponse(
        id=notification.id,
        category=notification.category,
        kind=notification.kind,
        title=notification.title,
        body=notification.body,
        data=notification.data,
        read=notification.read_at is not None,
        created_at=notification.created_at,
    )


# ---------- inbox ----------


@router.get("")
async def list_notifications(
    user: CurrentUser,
    db: DbSession,
    unread_only: bool = False,
    before: datetime | None = None,
    limit: Annotated[int, Query(ge=1, le=100)] = 50,
) -> list[NotificationResponse]:
    """Newest first. For the next page, pass the last item's created_at as before."""
    notifications = await service.list_notifications(
        db, user.id, unread_only=unread_only, before=before, limit=limit
    )
    return [_to_response(n) for n in notifications]


@router.get("/unread-count")
async def unread_count(user: CurrentUser, db: DbSession) -> UnreadCountResponse:
    """For the badge on the app's bell icon."""
    return UnreadCountResponse(unread=await service.count_unread(db, user.id))


@router.post("/read-all")
async def mark_all_read(user: CurrentUser, db: DbSession) -> MarkedReadResponse:
    return MarkedReadResponse(marked=await service.mark_all_read(db, user.id))


@router.post("/{notification_id}/read")
async def mark_read(
    notification_id: uuid.UUID, user: CurrentUser, db: DbSession
) -> NotificationResponse:
    try:
        notification = await service.mark_read(db, user.id, notification_id)
    except service.NotificationNotFoundError:
        raise _NOT_FOUND from None
    return _to_response(notification)


@router.delete("/{notification_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_notification(
    notification_id: uuid.UUID, user: CurrentUser, db: DbSession
) -> Response:
    try:
        await service.delete_notification(db, user.id, notification_id)
    except service.NotificationNotFoundError:
        raise _NOT_FOUND from None
    return Response(status_code=status.HTTP_204_NO_CONTENT)


# ---------- settings ----------


@router.get("/preferences")
async def list_preferences(
    user: CurrentUser, db: DbSession
) -> list[PreferenceResponse]:
    """How each category of alert reaches the user. Every notification also
    goes to the in-app inbox; that cannot be switched off."""
    return [
        PreferenceResponse(category=category, push=channels.push, email=channels.email)
        for category, channels in await service.list_channels(db, user.id)
    ]


@router.patch("/preferences/{category}")
async def update_preference(
    category: NotificationCategory,
    data: PreferenceUpdateRequest,
    user: CurrentUser,
    db: DbSession,
) -> PreferenceResponse:
    """Push alerts also need the device's own switch (PATCH /devices/{id},
    notifications_enabled) and a push token."""
    channels = await service.update_channels(
        db, user.id, category, push=data.push, email=data.email
    )
    return PreferenceResponse(
        category=category, push=channels.push, email=channels.email
    )


@router.post("/test")
async def send_test_push(user: CurrentUser, db: DbSession) -> TestPushResponse:
    """Send a test alert to every device with alerts on, to check the setup."""
    devices, delivered = await service.send_test_push(db, user.id)
    return TestPushResponse(devices=devices, delivered=delivered)
