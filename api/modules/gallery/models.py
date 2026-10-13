"""SQLAlchemy ORM models for the gallery module.

Face photos and embeddings are biometric data: a person is only created after
the owner confirms consent, and deleting a person removes every photo, file
and embedding of theirs for good.
"""

import uuid
from datetime import UTC, datetime
from enum import StrEnum

from sqlalchemy import (
    JSON,
    BigInteger,
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


class PersonCategory(StrEnum):
    FAMILY = "family"
    STAFF = "staff"
    VISITOR = "visitor"
    BLOCKED = "blocked"  # someone the owner wants an urgent alert about


class FaceStatus(StrEnum):
    PENDING = "pending"  # stored, waiting for an agent to compute its embedding
    READY = "ready"  # has an embedding for at least one face model
    REJECTED = "rejected"  # an agent found no usable face in the photo


class Person(Base):
    __tablename__ = "gallery_people"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    owner_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("users.id", ondelete="CASCADE"), index=True
    )
    name: Mapped[str] = mapped_column(String(100))
    category: Mapped[str] = mapped_column(String(16))
    notes: Mapped[str | None] = mapped_column(String(500))
    consent_confirmed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, onupdate=_utcnow
    )


class Face(Base):
    """One reference photo of a person."""

    __tablename__ = "gallery_faces"
    __table_args__ = (
        # Uploading the same photo twice for a person keeps one copy.
        UniqueConstraint("person_id", "sha256"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    person_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("gallery_people.id", ondelete="CASCADE"), index=True
    )
    # Copied from the person so agents' queries need no join.
    owner_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("users.id", ondelete="CASCADE"), index=True
    )
    status: Mapped[str] = mapped_column(String(16), default=FaceStatus.PENDING)
    reject_reason: Mapped[str | None] = mapped_column(String(200))
    content_type: Mapped[str] = mapped_column(String(32))
    size_bytes: Mapped[int] = mapped_column(BigInteger)
    sha256: Mapped[str] = mapped_column(String(64))
    storage_key: Mapped[str] = mapped_column(String(255), unique=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow
    )


class FaceEmbedding(Base):
    """A face's vector as computed by one face model.

    Vectors from different models cannot be compared, so they are stored per
    model. The first agent to compute one shares it with all the owner's other
    agents running the same model.
    """

    __tablename__ = "face_embeddings"
    __table_args__ = (UniqueConstraint("face_id", "model"),)

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    face_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("gallery_faces.id", ondelete="CASCADE"), index=True
    )
    model: Mapped[str] = mapped_column(String(64))
    vector: Mapped[list[float]] = mapped_column(JSON)
    computed_by_agent_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("edge_agents.id", ondelete="SET NULL")
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow
    )
