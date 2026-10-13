"""SQLAlchemy ORM models for the audit module."""

import uuid
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any

from sqlalchemy import JSON, Boolean, DateTime, ForeignKey, Index, String, Uuid
from sqlalchemy.orm import Mapped, mapped_column

from api.core.database import Base


def _utcnow() -> datetime:
    return datetime.now(UTC)


class AuditActor(StrEnum):
    USER = "user"  # a logged-in person
    AGENT = "agent"  # an edge agent, acting with its own token
    ANONYMOUS = "anonymous"  # not logged in, e.g. a login attempt


class AuditAction(StrEnum):
    # auth
    SIGNUP = "signup"
    EMAIL_VERIFIED = "email_verified"
    PASSWORD_RESET_REQUESTED = "password_reset_requested"
    PASSWORD_RESET = "password_reset"
    LOGIN_SUCCEEDED = "login_succeeded"
    LOGIN_FAILED = "login_failed"
    # device_link
    DEVICE_LINKED = "device_linked"
    DEVICE_UPDATED = "device_updated"
    DEVICE_LOGGED_OUT = "device_logged_out"
    DEVICE_REMOVED = "device_removed"
    DEVICES_LOGGED_OUT_ALL = "devices_logged_out_all"
    DEVICE_EVICTED = "device_evicted"  # logged out to make room for a new one
    PUSH_TOKEN_SET = "push_token_set"
    PUSH_TOKEN_CLEARED = "push_token_cleared"
    REFRESH_TOKEN_REUSED = "refresh_token_reused"
    # edge_agent
    AGENT_CREATED = "agent_created"
    AGENT_RENAMED = "agent_renamed"
    AGENT_ENROLLMENT_TOKEN_ISSUED = "agent_enrollment_token_issued"
    AGENT_ENROLLED = "agent_enrolled"
    AGENT_ENROLLMENT_FAILED = "agent_enrollment_failed"
    AGENT_REVOKED = "agent_revoked"
    AGENT_DELETED = "agent_deleted"
    # cameras
    CAMERA_ADDED = "camera_added"
    CAMERA_ADOPTED = "camera_adopted"
    CAMERA_IGNORED = "camera_ignored"
    CAMERA_UPDATED = "camera_updated"
    CAMERA_DELETED = "camera_deleted"
    # media_access
    MEDIA_LINK_CREATED = "media_link_created"
    MEDIA_DELETED = "media_deleted"
    # gallery
    PERSON_CREATED = "person_created"
    PERSON_UPDATED = "person_updated"
    PERSON_DELETED = "person_deleted"
    FACE_ADDED = "face_added"
    FACE_DELETED = "face_deleted"
    FACE_LINK_CREATED = "face_link_created"
    # notifications
    NOTIFICATION_SETTINGS_UPDATED = "notification_settings_updated"
    # events
    EVENT_DELETED = "event_deleted"
    # account
    ACCOUNT_DELETED = "account_deleted"
    ACCOUNT_DELETION_FAILED = "account_deletion_failed"


class AuditEntry(Base):
    """One security-relevant action. Rows are only ever added, never changed;
    the retention job is the only thing that deletes them."""

    __tablename__ = "audit_log"
    __table_args__ = (Index("ix_audit_log_user_created", "user_id", "created_at"),)

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    # The account the action concerns. Kept if the account goes; empty for a
    # login attempt on an unknown email.
    user_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("users.id", ondelete="SET NULL")
    )
    actor: Mapped[str] = mapped_column(String(16))
    # Set when actor is "agent". No foreign key: the entry outlives the agent.
    agent_id: Mapped[uuid.UUID | None] = mapped_column(Uuid)
    action: Mapped[str] = mapped_column(String(64), index=True)
    success: Mapped[bool] = mapped_column(Boolean, default=True)
    # What was acted on, e.g. ("camera", <camera id>). No foreign key either.
    target_type: Mapped[str | None] = mapped_column(String(32))
    target_id: Mapped[uuid.UUID | None] = mapped_column(Uuid)
    # Small extra facts (a reason, changed field names). Never secrets.
    details: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    ip: Mapped[str | None] = mapped_column(String(45))
    user_agent: Mapped[str | None] = mapped_column(String(255))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, index=True
    )
