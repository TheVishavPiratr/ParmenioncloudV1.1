"""Pydantic request/response schemas for the media_access module."""

import uuid
from datetime import UTC, datetime, timedelta
from typing import Annotated, Self

from pydantic import AwareDatetime, BaseModel, Field, StringConstraints, model_validator

from api.modules.media_access.models import MediaKind

MB = 1024 * 1024
MAX_BYTES = {MediaKind.SNAPSHOT: 10 * MB, MediaKind.CLIP: 200 * MB}
CONTENT_TYPES = {
    MediaKind.SNAPSHOT: {"image/jpeg", "image/png", "image/webp"},
    MediaKind.CLIP: {"video/mp4", "video/webm"},
}
MAX_CLIP_SECONDS = 3600
# Agent clocks may run a little fast.
CLOCK_SKEW = timedelta(minutes=5)

Sha256 = Annotated[str, StringConstraints(to_lower=True, pattern=r"^[0-9a-fA-F]{64}$")]

# ---------- agent-facing (/agent/media) ----------


class MediaCreateRequest(BaseModel):
    """Announce a file before uploading it, so the cloud can check it arrives
    complete and unchanged."""

    camera_id: uuid.UUID
    kind: MediaKind
    content_type: str
    size_bytes: int = Field(ge=1)
    sha256: Sha256
    captured_at: AwareDatetime
    duration_s: float | None = Field(default=None, gt=0, le=MAX_CLIP_SECONDS)

    @model_validator(mode="after")
    def _fits_kind(self) -> Self:
        if self.content_type not in CONTENT_TYPES[self.kind]:
            allowed = ", ".join(sorted(CONTENT_TYPES[self.kind]))
            raise ValueError(f"content_type for a {self.kind} must be one of {allowed}")
        if self.size_bytes > MAX_BYTES[self.kind]:
            raise ValueError(
                f"a {self.kind} can be at most {MAX_BYTES[self.kind] // MB} MB"
            )
        if self.kind == MediaKind.SNAPSHOT and self.duration_s is not None:
            raise ValueError("a snapshot has no duration")
        if self.captured_at > datetime.now(UTC) + CLOCK_SKEW:
            raise ValueError("captured_at is in the future")
        return self


class MediaUploadResponse(BaseModel):
    media_id: uuid.UUID
    # "ready" means this file was already uploaded: nothing left to send.
    status: str
    # PUT the raw file bytes here (no multipart), with the agent token.
    upload_url: str


# ---------- user-facing (/media) ----------


class MediaResponse(BaseModel):
    id: uuid.UUID
    camera_id: uuid.UUID | None
    agent_id: uuid.UUID | None
    kind: str
    content_type: str
    size_bytes: int
    sha256: str
    duration_s: float | None
    captured_at: datetime
    uploaded_at: datetime | None


class MediaLinkResponse(BaseModel):
    # Relative to the API base URL. Works without a login header (so <img> and
    # <video> tags can use it) until expires_at, and supports seeking.
    url: str
    expires_at: datetime
