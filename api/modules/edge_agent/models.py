"""SQLAlchemy ORM models for the edge_agent module."""

import uuid
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any

from sqlalchemy import JSON, Boolean, DateTime, ForeignKey, Integer, String, Uuid
from sqlalchemy.orm import Mapped, mapped_column

from api.core.database import Base


def _utcnow() -> datetime:
    return datetime.now(UTC)


class AgentStatus(StrEnum):
    PENDING = "pending"  # created, waiting for the box to enroll
    ACTIVE = "active"  # enrolled, holds a working agent token
    REVOKED = "revoked"  # token cancelled by the owner


class EdgeAgent(Base):
    """An on-site box running the Edge Agent and MediaMTX.

    Tokens are stored only as SHA-256 hashes; the plain values are shown once.
    """

    __tablename__ = "edge_agents"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    owner_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("users.id", ondelete="CASCADE"), index=True
    )
    name: Mapped[str] = mapped_column(String(100))
    status: Mapped[str] = mapped_column(String(16), default=AgentStatus.PENDING)

    enrollment_token_hash: Mapped[str | None] = mapped_column(String(64), unique=True)
    enrollment_expires_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True)
    )
    token_hash: Mapped[str | None] = mapped_column(String(64), unique=True)
    enrolled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    # Reported by the agent at enrollment and on every heartbeat.
    last_seen_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_ip: Mapped[str | None] = mapped_column(String(45))
    hostname: Mapped[str | None] = mapped_column(String(255))
    platform: Mapped[str | None] = mapped_column(String(64))
    software_version: Mapped[str | None] = mapped_column(String(64))
    mediamtx_running: Mapped[bool | None] = mapped_column(Boolean)
    mediamtx_version: Mapped[str | None] = mapped_column(String(64))
    last_health: Mapped[dict[str, Any] | None] = mapped_column(JSON)

    # Bumped whenever what the agent should do changes; the agent re-fetches
    # its config when the number it holds is out of date.
    config_version: Mapped[int] = mapped_column(Integer, default=1)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, onupdate=_utcnow
    )


class CommandType(StrEnum):
    CONFIG_CHANGED = "config_changed"
    RESCAN_CAMERAS = "rescan_cameras"


class CommandStatus(StrEnum):
    PENDING = "pending"  # queued, not yet pushed over the WebSocket
    SENT = "sent"  # pushed, waiting for the agent's ack
    ACKED = "acked"  # agent reports success
    FAILED = "failed"  # agent reports it could not carry it out
    EXPIRED = "expired"  # never acked in time, or cancelled


class AgentCommand(Base):
    """An instruction from the cloud to one agent, kept until the agent acks it."""

    __tablename__ = "agent_commands"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    agent_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("edge_agents.id", ondelete="CASCADE"), index=True
    )
    type: Mapped[str] = mapped_column(String(32))
    payload: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    status: Mapped[str] = mapped_column(String(16), default=CommandStatus.PENDING)
    error: Mapped[str | None] = mapped_column(String(500))
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow
    )
    sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    acked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
