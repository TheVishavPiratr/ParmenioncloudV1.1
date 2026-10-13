"""Pydantic request/response schemas for the notifications module."""

import uuid
from datetime import datetime

from pydantic import BaseModel

from api.modules.notifications.models import NotificationCategory


class NotificationResponse(BaseModel):
    id: uuid.UUID
    category: str
    kind: str
    title: str
    body: str
    data: dict[str, str] | None
    read: bool
    created_at: datetime


class UnreadCountResponse(BaseModel):
    unread: int


class MarkedReadResponse(BaseModel):
    marked: int


class PreferenceResponse(BaseModel):
    category: NotificationCategory
    push: bool
    email: bool


class PreferenceUpdateRequest(BaseModel):
    """Leave a channel out to keep its current setting."""

    push: bool | None = None
    email: bool | None = None


class TestPushResponse(BaseModel):
    devices: int
    delivered: int
