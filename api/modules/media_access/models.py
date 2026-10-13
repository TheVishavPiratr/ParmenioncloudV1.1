"""SQLAlchemy ORM models for the media_access module."""

import uuid
from datetime import UTC, datetime
from enum import StrEnum

from sqlalchemy import (
    BigInteger,
    DateTime,
    Float,
    ForeignKey,
    String,
    UniqueConstraint,
    Uuid,
)
from sqlalchemy.orm import Mapped, mapped_column

from api.core.database import Base


def _utcnow() -> datetime:
    return datetime.now(UTC)


class MediaKind(StrEnum):
    SNAPSHOT = "snapshot"
    CLIP = "clip"


class MediaStatus(StrEnum):
    PENDING = "pending"  # registered by the agent, file not uploaded yet
    READY = "ready"  # file stored in the cloud and checked against its hash


class Media(Base):
    """A snapshot or video clip uploaded once by an edge agent.

    After the upload the cloud does everything else: storing, access checks,
    serving (with seeking) and deleting after the retention period. The agent
    can drop its local copy as soon as the upload succeeds.
    """

    __tablename__ = "media"
    __table_args__ = (
        # An agent retrying a registration gets the same row back.
        UniqueConstraint("camera_id", "sha256"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    owner_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("users.id", ondelete="CASCADE"), index=True
    )
    # Footage outlives its camera or agent being removed; retention deletes it.
    agent_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("edge_agents.id", ondelete="SET NULL"), index=True
    )
    camera_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("cameras.id", ondelete="SET NULL"), index=True
    )
    kind: Mapped[str] = mapped_column(String(16))
    content_type: Mapped[str] = mapped_column(String(32))
    status: Mapped[str] = mapped_column(String(16), default=MediaStatus.PENDING)
    size_bytes: Mapped[int] = mapped_column(BigInteger)
    sha256: Mapped[str] = mapped_column(String(64))
    storage_key: Mapped[str] = mapped_column(String(255), unique=True)
    duration_s: Mapped[float | None] = mapped_column(Float)
    captured_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    uploaded_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow
    )
