"""Pydantic request/response schemas for the events module."""

import uuid
from datetime import UTC, datetime, timedelta
from typing import Annotated, Literal, Self

from pydantic import (
    AwareDatetime,
    BaseModel,
    ConfigDict,
    Field,
    StringConstraints,
    model_validator,
)

from api.modules.events.models import EventType

MAX_EVENTS_PER_BATCH = 100
# Agent clocks may run a little fast.
CLOCK_SKEW = timedelta(minutes=5)

ClientEventId = Annotated[
    str, StringConstraints(min_length=1, max_length=64, pattern=r"^[A-Za-z0-9._:-]+$")
]
ModelName = Annotated[
    str, StringConstraints(min_length=1, max_length=64, pattern=r"^[A-Za-z0-9._-]+$")
]
Fraction = Annotated[float, Field(ge=0, le=1, allow_inf_nan=False)]

# ---------- agent-facing (/agent/events) ----------


class EventDetails(BaseModel):
    """Extras the app can draw. Unknown fields are refused, so details can't
    become a dumping ground."""

    model_config = ConfigDict(extra="forbid")

    # x, y, width, height as fractions of the frame.
    bbox: Annotated[list[Fraction], Field(min_length=4, max_length=4)] | None = None
    # The agent's tracker id, linking events about the same object.
    track_id: ClientEventId | None = None


class AgentEvent(BaseModel):
    client_event_id: ClientEventId
    camera_id: uuid.UUID
    type: EventType
    occurred_at: AwareDatetime
    person_id: uuid.UUID | None = None
    confidence: Fraction | None = None
    face_model: ModelName | None = None
    # Register the file with POST /agent/media first; it may still be uploading.
    snapshot_media_id: uuid.UUID | None = None
    clip_media_id: uuid.UUID | None = None
    details: EventDetails | None = None

    @model_validator(mode="after")
    def _consistent(self) -> Self:
        recognized = self.type == EventType.FACE_RECOGNIZED
        if recognized and self.person_id is None:
            raise ValueError("face_recognized needs person_id")
        if not recognized and self.person_id is not None:
            raise ValueError("person_id is only for face_recognized")
        if self.face_model is not None and self.type not in (
            EventType.FACE_RECOGNIZED,
            EventType.FACE_UNKNOWN,
        ):
            raise ValueError("face_model is only for face events")
        if self.occurred_at > datetime.now(UTC) + CLOCK_SKEW:
            raise ValueError("occurred_at is in the future")
        return self


class AgentEventBatch(BaseModel):
    events: list[AgentEvent] = Field(min_length=1, max_length=MAX_EVENTS_PER_BATCH)


class AgentEventResult(BaseModel):
    client_event_id: str
    # duplicate: received before, event_id is the first one.
    # rejected: see reason; resending it unchanged won't help.
    status: Literal["created", "duplicate", "rejected"]
    event_id: uuid.UUID | None
    # camera_not_found, media_not_found, or too_old.
    reason: str | None = None


class AgentEventBatchResponse(BaseModel):
    results: list[AgentEventResult]


class AgentEventMediaRequest(BaseModel):
    """Attach a file that was not ready when the event was reported."""

    snapshot_media_id: uuid.UUID | None = None
    clip_media_id: uuid.UUID | None = None

    @model_validator(mode="after")
    def _something(self) -> Self:
        if self.snapshot_media_id is None and self.clip_media_id is None:
            raise ValueError("send snapshot_media_id or clip_media_id")
        return self


class AgentEventMediaResponse(BaseModel):
    event_id: uuid.UUID
    snapshot_media_id: uuid.UUID | None
    clip_media_id: uuid.UUID | None


# ---------- user-facing (/events) ----------


class EventPerson(BaseModel):
    id: uuid.UUID
    name: str
    category: str


class EventResponse(BaseModel):
    id: uuid.UUID
    camera_id: uuid.UUID | None
    agent_id: uuid.UUID | None
    type: str
    severity: str
    # Null for a recognised person who has since been removed from the gallery.
    person: EventPerson | None
    confidence: float | None
    face_model: str | None
    # View with POST /media/{id}/link; 404 there while the upload is unfinished.
    snapshot_media_id: uuid.UUID | None
    clip_media_id: uuid.UUID | None
    details: EventDetails | None
    occurred_at: datetime
    received_at: datetime
    reviewed: bool


class EventUpdateRequest(BaseModel):
    reviewed: bool


class UnreviewedCountResponse(BaseModel):
    unreviewed: int


class ReviewedAllResponse(BaseModel):
    reviewed: int
