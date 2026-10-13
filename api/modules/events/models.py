"""SQLAlchemy ORM models for the events module."""

import uuid
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any

from sqlalchemy import (
    JSON,
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Index,
    String,
    UniqueConstraint,
    Uuid,
)
from sqlalchemy.orm import Mapped, mapped_column

from api.core.database import Base


def _utcnow() -> datetime:
    return datetime.now(UTC)


class EventType(StrEnum):
    MOTION = "motion"
    PERSON_DETECTED = "person_detected"  # a body, no face matched or seen
    FACE_RECOGNIZED = "face_recognized"  # matched someone in the gallery
    FACE_UNKNOWN = "face_unknown"  # a face that matched nobody
    CAMERA_TAMPERED = "camera_tampered"  # covered, moved or blinded


class EventSeverity(StrEnum):
    INFO = "info"
    WARNING = "warning"
    CRITICAL = "critical"  # a blocked person was seen


class Event(Base):
    """Something an edge agent saw on a camera at a given moment.

    Holds no biometric data: a recognised face is only a reference to a
    gallery person. Snapshots and clips live in media_access and are linked
    by id. Severity is decided by the cloud, never taken from the agent.
    """

    __tablename__ = "events"
    __table_args__ = (
        # An agent retrying a batch gets the events it already created back.
        UniqueConstraint("agent_id", "client_event_id"),
        Index("ix_events_owner_occurred", "owner_id", "occurred_at"),
        # Finds the last alert for the same thing on the same camera.
        Index("ix_events_alert_lookup", "camera_id", "type", "occurred_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    owner_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("users.id", ondelete="CASCADE")
    )
    # Events outlive their agent, camera or person; retention deletes them.
    agent_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("edge_agents.id", ondelete="SET NULL")
    )
    camera_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("cameras.id", ondelete="SET NULL")
    )
    client_event_id: Mapped[str] = mapped_column(String(64))
    type: Mapped[str] = mapped_column(String(32))
    severity: Mapped[str] = mapped_column(String(16))

    person_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("gallery_people.id", ondelete="SET NULL"), index=True
    )
    confidence: Mapped[float | None] = mapped_column(Float)
    face_model: Mapped[str | None] = mapped_column(String(64))

    snapshot_media_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("media.id", ondelete="SET NULL")
    )
    clip_media_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("media.id", ondelete="SET NULL")
    )
    # Small, schema-checked extras such as a bounding box. Never free-form.
    details: Mapped[dict[str, Any] | None] = mapped_column(JSON)

    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    received_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow
    )
    # True when this event caused an alert; drives the alert cooldown.
    notified: Mapped[bool] = mapped_column(Boolean, default=False)
    reviewed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
