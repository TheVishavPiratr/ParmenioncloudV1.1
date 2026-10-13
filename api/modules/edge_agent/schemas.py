"""Pydantic request/response schemas for the edge_agent module."""

import uuid
from datetime import datetime
from typing import Annotated, Literal

from pydantic import BaseModel, Field, StringConstraints

# Single-line text: no control characters. Besides keeping logs clean, this
# matters because PostgreSQL rejects NUL (\x00) in text columns outright.
_NO_CONTROL_CHARS = r"^[^\x00-\x1f\x7f]*$"


def _line(max_length: int, *, strip: bool = False) -> object:
    return StringConstraints(
        strip_whitespace=strip,
        min_length=1,
        max_length=max_length,
        pattern=_NO_CONTROL_CHARS,
    )


AgentName = Annotated[str, _line(100, strip=True)]
Hostname = Annotated[str, _line(255)]
ShortText = Annotated[str, _line(64)]
Percent = Annotated[float, Field(ge=0, le=100)]

# ---------- user-facing (/agents) ----------


class AgentCreateRequest(BaseModel):
    name: AgentName


class AgentUpdateRequest(BaseModel):
    name: AgentName


class AgentHealth(BaseModel):
    uptime_s: int | None = None
    cpu_pct: float | None = None
    mem_pct: float | None = None
    disk_pct: float | None = None
    mediamtx_active_paths: int | None = None


class MediaMtxStatus(BaseModel):
    running: bool | None
    version: str | None


class AgentResponse(BaseModel):
    id: uuid.UUID
    name: str
    status: str
    online: bool
    last_seen_at: datetime | None
    last_ip: str | None
    hostname: str | None
    platform: str | None
    software_version: str | None
    mediamtx: MediaMtxStatus
    health: AgentHealth | None
    config_version: int
    enrolled_at: datetime | None
    created_at: datetime


class EnrollmentTokenResponse(BaseModel):
    enrollment_token: str
    enrollment_expires_at: datetime


class AgentCreatedResponse(EnrollmentTokenResponse):
    agent: AgentResponse


class CommandQueuedResponse(BaseModel):
    command_id: uuid.UUID


# ---------- agent-facing (/agent) ----------


class EnrollRequest(BaseModel):
    enrollment_token: Annotated[str, _line(200)]
    hostname: Hostname
    platform: ShortText
    software_version: ShortText


class EnrollResponse(BaseModel):
    agent_id: uuid.UUID
    agent_token: str
    heartbeat_interval_seconds: int


class MediaMtxHeartbeat(BaseModel):
    running: bool
    version: ShortText | None = None
    active_paths: int | None = Field(default=None, ge=0)


class HeartbeatRequest(BaseModel):
    software_version: ShortText
    uptime_s: int = Field(ge=0)
    cpu_pct: Percent | None = None
    mem_pct: Percent | None = None
    disk_pct: Percent | None = None
    mediamtx: MediaMtxHeartbeat


class HeartbeatResponse(BaseModel):
    server_time: datetime
    config_version: int
    heartbeat_interval_seconds: int


class AgentSelfResponse(BaseModel):
    agent_id: uuid.UUID
    name: str
    status: str
    config_version: int
    heartbeat_interval_seconds: int


# ---------- WebSocket messages (agent -> cloud) ----------


class AckMessage(BaseModel):
    type: Literal["ack"]
    id: uuid.UUID
    ok: bool
    # Free text from the agent: may span lines, but never contain NUL.
    error: str | None = Field(default=None, max_length=500, pattern=r"^[^\x00]*$")
