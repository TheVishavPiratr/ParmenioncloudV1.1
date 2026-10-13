"""Pydantic request/response schemas for the audit module."""

import uuid
from datetime import datetime
from typing import Any

from pydantic import BaseModel


class AuditEntryResponse(BaseModel):
    id: uuid.UUID
    action: str
    success: bool
    actor: str
    agent_id: uuid.UUID | None
    target_type: str | None
    target_id: uuid.UUID | None
    details: dict[str, Any] | None
    ip: str | None
    user_agent: str | None
    created_at: datetime
