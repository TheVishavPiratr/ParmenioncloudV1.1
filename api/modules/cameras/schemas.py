"""Pydantic request/response schemas for the cameras module."""

import uuid
from datetime import datetime
from ipaddress import IPv4Address, IPv6Address
from typing import Annotated, Literal, Self
from urllib.parse import urlsplit

from pydantic import (
    AfterValidator,
    BaseModel,
    Field,
    StringConstraints,
    model_validator,
)

# Single-line text: no control characters. PostgreSQL also rejects NUL (\x00)
# in text columns outright.
_NO_CONTROL_CHARS = r"^[^\x00-\x1f\x7f]*$"
# URLs additionally may not contain spaces.
_NO_CONTROL_CHARS_OR_SPACES = r"^[^\x00-\x20\x7f]*$"


def _line(max_length: int, *, strip: bool = False) -> object:
    return StringConstraints(
        strip_whitespace=strip,
        min_length=1,
        max_length=max_length,
        pattern=_NO_CONTROL_CHARS,
    )


def _check_rtsp_url(url: str) -> str:
    parts = urlsplit(url)
    if parts.scheme.lower() not in ("rtsp", "rtsps"):
        raise ValueError("must start with rtsp:// or rtsps://")
    if "@" in parts.netloc:
        raise ValueError("put credentials in username and password, not the URL")
    if not parts.hostname:
        raise ValueError("must include a host")
    _ = parts.port  # raises ValueError for a port that is not a number 0-65535
    return url


CameraName = Annotated[str, _line(100, strip=True)]
RtspUrl = Annotated[
    str,
    StringConstraints(
        strip_whitespace=True,
        min_length=1,
        max_length=500,
        pattern=_NO_CONTROL_CHARS_OR_SPACES,
    ),
    AfterValidator(_check_rtsp_url),
]
Username = Annotated[str, _line(100)]
# Passwords may contain spaces and are not stripped.
Password = Annotated[str, _line(128)]
ShortText = Annotated[str, _line(64)]

# ---------- user-facing (/cameras) ----------


class CameraCreateRequest(BaseModel):
    """Add a camera by hand, without a network scan."""

    agent_id: uuid.UUID
    name: CameraName
    rtsp_url: RtspUrl
    username: Username | None = None
    password: Password | None = None
    record_enabled: bool = False
    detection_enabled: bool = False


class CameraAdoptRequest(BaseModel):
    """Set up a discovered camera. rtsp_url defaults to its first found stream."""

    name: CameraName
    rtsp_url: RtspUrl | None = None
    username: Username | None = None
    password: Password | None = None
    record_enabled: bool = False
    detection_enabled: bool = False


class CameraUpdateRequest(BaseModel):
    """Only the fields sent are changed. username or password null clears it."""

    name: CameraName | None = None
    rtsp_url: RtspUrl | None = None
    username: Username | None = None
    password: Password | None = None
    enabled: bool | None = None
    record_enabled: bool | None = None
    detection_enabled: bool | None = None

    @model_validator(mode="after")
    def _required_fields_not_null(self) -> Self:
        for field in (
            "name",
            "rtsp_url",
            "enabled",
            "record_enabled",
            "detection_enabled",
        ):
            if field in self.model_fields_set and getattr(self, field) is None:
                raise ValueError(f"{field} cannot be null")
        return self


class CameraResponse(BaseModel):
    id: uuid.UUID
    agent_id: uuid.UUID
    name: str | None
    state: str
    online: bool
    ip_address: str | None
    vendor: str | None
    model: str | None
    discovered_streams: list[str]
    rtsp_url: str | None
    username: str | None
    has_password: bool
    stream_path: str
    record_enabled: bool
    detection_enabled: bool
    stream_state: str
    stream_error: str | None
    stream_checked_at: datetime | None
    last_discovered_at: datetime | None
    created_at: datetime


# ---------- agent-facing (/agent) ----------


class AgentCameraConfig(BaseModel):
    id: uuid.UUID
    stream_path: str
    rtsp_url: str
    username: str | None
    password: str | None
    record: bool
    detection: bool


class AgentConfigResponse(BaseModel):
    config_version: int
    cameras: list[AgentCameraConfig]


class DiscoveredDevice(BaseModel):
    # Stable id of the device on the agent's network, e.g. MAC or ONVIF serial.
    device_key: Annotated[str, _line(128)]
    ip_address: IPv4Address | IPv6Address | None = None
    vendor: ShortText | None = None
    model: ShortText | None = None
    streams: list[RtspUrl] = Field(default_factory=list, max_length=8)


class DiscoveryReport(BaseModel):
    devices: list[DiscoveredDevice] = Field(max_length=256)


class DiscoveryResult(BaseModel):
    new: int
    updated: int
    # Not stored because the agent already has too many cameras on record.
    skipped: int


class CameraStatus(BaseModel):
    camera_id: uuid.UUID
    state: Literal["streaming", "error"]
    # Free text from the agent: may span lines, but never contain NUL.
    error: str | None = Field(default=None, max_length=500, pattern=r"^[^\x00]*$")


class StatusReport(BaseModel):
    cameras: list[CameraStatus] = Field(max_length=64)


class StatusResult(BaseModel):
    # Reports for unknown, other agents', or inactive cameras are skipped.
    updated: int
