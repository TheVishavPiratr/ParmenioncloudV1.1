"""SQLAlchemy ORM models for the device_link module."""

import uuid
from datetime import UTC, datetime
from enum import StrEnum

from sqlalchemy import (
    Boolean,
    DateTime,
    ForeignKey,
    Integer,
    String,
    UniqueConstraint,
    Uuid,
)
from sqlalchemy.orm import Mapped, mapped_column

from api.core.database import Base


def _utcnow() -> datetime:
    return datetime.now(UTC)


class Platform(StrEnum):
    ANDROID = "android"
    IOS = "ios"
    WEB = "web"


class PushProvider(StrEnum):
    FCM = "fcm"  # Firebase Cloud Messaging: Android and iOS


class LinkedDevice(Base):
    """A phone, tablet or browser the user is logged in on.

    It holds a long-lived refresh token (stored only as a SHA-256 hash) that is
    swapped for a new one on every use. Logging the device out deletes the row.
    """

    __tablename__ = "linked_devices"
    __table_args__ = (
        # One push token belongs to one device: a phone that switches accounts
        # stops getting the old account's alerts.
        UniqueConstraint("push_provider", "push_token"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    user_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("users.id", ondelete="CASCADE"), index=True
    )
    name: Mapped[str] = mapped_column(String(100))
    platform: Mapped[str] = mapped_column(String(16))
    app_version: Mapped[str | None] = mapped_column(String(64))
    os_version: Mapped[str | None] = mapped_column(String(64))

    refresh_token_hash: Mapped[str] = mapped_column(String(64), unique=True)
    # The token before the last swap. Seeing it again means it was copied.
    previous_token_hash: Mapped[str | None] = mapped_column(String(64), index=True)
    refresh_expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    # The user's session_version at login; a password reset ends the session.
    session_version: Mapped[int] = mapped_column(Integer)

    push_provider: Mapped[str | None] = mapped_column(String(16))
    push_token: Mapped[str | None] = mapped_column(String(512))
    notifications_enabled: Mapped[bool] = mapped_column(Boolean, default=True)

    last_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    last_ip: Mapped[str | None] = mapped_column(String(45))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow
    )
