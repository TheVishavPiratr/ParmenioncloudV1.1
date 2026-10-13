"""SQLAlchemy ORM models for the cameras module."""

import uuid
from datetime import UTC, datetime
from enum import StrEnum

from sqlalchemy import (
    JSON,
    Boolean,
    DateTime,
    ForeignKey,
    String,
    UniqueConstraint,
    Uuid,
)
from sqlalchemy.orm import Mapped, mapped_column

from api.core.database import Base


def _utcnow() -> datetime:
    return datetime.now(UTC)


class CameraState(StrEnum):
    DISCOVERED = "discovered"  # found by a scan, not set up by the owner yet
    ACTIVE = "active"  # set up and sent to the agent
    DISABLED = "disabled"  # set up, but kept off the agent for now
    IGNORED = "ignored"  # dismissed by the owner; rescans don't bring it back


class StreamState(StrEnum):
    UNKNOWN = "unknown"  # no report from the agent since the last change
    STREAMING = "streaming"
    ERROR = "error"


class Camera(Base):
    """One camera on the network of an edge agent.

    The password is stored encrypted (see credentials.py), never in plain
    text, because the agent needs the real value back to open the stream.
    """

    __tablename__ = "cameras"
    __table_args__ = (
        # A rescan updates the row it found before instead of adding another.
        UniqueConstraint("agent_id", "device_key"),
        UniqueConstraint("agent_id", "stream_path"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    # Copied from the agent so this module never has to read edge_agents.
    owner_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("users.id", ondelete="CASCADE"), index=True
    )
    agent_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("edge_agents.id", ondelete="CASCADE"), index=True
    )
    state: Mapped[str] = mapped_column(String(16))
    name: Mapped[str | None] = mapped_column(String(100))

    # Reported by the agent's network scan. NULL for cameras added by hand.
    device_key: Mapped[str | None] = mapped_column(String(128))
    ip_address: Mapped[str | None] = mapped_column(String(45))
    vendor: Mapped[str | None] = mapped_column(String(64))
    model: Mapped[str | None] = mapped_column(String(64))
    discovered_streams: Mapped[list[str] | None] = mapped_column(JSON)
    last_discovered_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    # Chosen by the owner and sent to the agent while the camera is active.
    rtsp_url: Mapped[str | None] = mapped_column(String(500))
    username: Mapped[str | None] = mapped_column(String(100))
    password_encrypted: Mapped[str | None] = mapped_column(String(500))
    # MediaMTX path name on the agent. Set once, never changed.
    stream_path: Mapped[str] = mapped_column(String(32))
    record_enabled: Mapped[bool] = mapped_column(Boolean, default=False)
    detection_enabled: Mapped[bool] = mapped_column(Boolean, default=False)

    # Reported by the agent for active cameras.
    stream_state: Mapped[str] = mapped_column(String(16), default=StreamState.UNKNOWN)
    stream_error: Mapped[str | None] = mapped_column(String(500))
    stream_checked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, onupdate=_utcnow
    )
