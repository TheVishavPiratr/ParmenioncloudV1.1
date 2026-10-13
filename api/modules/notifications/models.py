"""SQLAlchemy ORM models for the notifications module."""

import uuid
from datetime import UTC, datetime
from enum import StrEnum

from sqlalchemy import JSON, Boolean, DateTime, ForeignKey, Index, String, Uuid
from sqlalchemy.orm import Mapped, mapped_column

from api.core.database import Base


def _utcnow() -> datetime:
    return datetime.now(UTC)


class NotificationCategory(StrEnum):
    """What a notification is about. Each category has its own settings."""

    SECURITY = "security"  # sign-ins, password changes, stolen sessions
    SYSTEM = "system"  # edge boxes and cameras
    DETECTION = "detection"  # people and motion seen by the cameras


class Notification(Base):
    """One message in a user's in-app inbox. Push and email are only ways of
    pointing the user at it; the inbox keeps it until the retention job."""

    __tablename__ = "notifications"
    __table_args__ = (Index("ix_notifications_user_created", "user_id", "created_at"),)

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    user_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("users.id", ondelete="CASCADE")
    )
    category: Mapped[str] = mapped_column(String(16))
    # What happened, e.g. "device_linked"; lets the app pick an icon or screen.
    kind: Mapped[str] = mapped_column(String(64))
    title: Mapped[str] = mapped_column(String(200))
    body: Mapped[str] = mapped_column(String(1000))
    # String ids the app needs to open the right screen. Never secrets.
    data: Mapped[dict[str, str] | None] = mapped_column(JSON)
    read_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, index=True
    )


class NotificationPreference(Base):
    """A user's choice of channels for one category. Without a row, the
    category's defaults apply."""

    __tablename__ = "notification_preferences"

    user_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("users.id", ondelete="CASCADE"), primary_key=True
    )
    category: Mapped[str] = mapped_column(String(16), primary_key=True)
    push: Mapped[bool] = mapped_column(Boolean)
    email: Mapped[bool] = mapped_column(Boolean)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow
    )
