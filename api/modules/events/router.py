"""HTTP endpoints for the events module.

Two audiences:
- agent_router (/agent/events): the Edge Agent reporting what it saw, by agent token.
- user_router  (/events):       people reviewing their timeline, by user JWT.
"""

import uuid
from datetime import datetime
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, Response, status
from sqlalchemy.ext.asyncio import AsyncSession

from api.core.database import get_db
from api.modules.auth.dependencies import CurrentUser
from api.modules.edge_agent.dependencies import CurrentAgent
from api.modules.events import service
from api.modules.events.models import Event, EventSeverity, EventType
from api.modules.events.schemas import (
    AgentEventBatch,
    AgentEventBatchResponse,
    AgentEventMediaRequest,
    AgentEventMediaResponse,
    EventDetails,
    EventPerson,
    EventResponse,
    EventUpdateRequest,
    ReviewedAllResponse,
    UnreviewedCountResponse,
)

agent_router = APIRouter(prefix="/agent/events", tags=["agent"])
user_router = APIRouter(prefix="/events", tags=["events"])

DbSession = Annotated[AsyncSession, Depends(get_db)]

_ERRORS: dict[type[service.EventError], tuple[int, str]] = {
    service.EventNotFoundError: (status.HTTP_404_NOT_FOUND, "Event not found"),
    service.MediaNotFoundError: (status.HTTP_404_NOT_FOUND, "Media not found"),
    service.MediaAlreadyAttachedError: (
        status.HTTP_409_CONFLICT,
        "The event already has a different file of that kind",
    ),
}


def _http_error(exc: service.EventError) -> HTTPException:
    code, detail = _ERRORS[type(exc)]
    return HTTPException(status_code=code, detail=detail)


def _to_response(event: Event, people: dict) -> EventResponse:
    person = people.get(event.person_id) if event.person_id else None
    return EventResponse(
        id=event.id,
        camera_id=event.camera_id,
        agent_id=event.agent_id,
        type=event.type,
        severity=event.severity,
        person=EventPerson(id=person.id, name=person.name, category=person.category)
        if person
        else None,
        confidence=event.confidence,
        face_model=event.face_model,
        snapshot_media_id=event.snapshot_media_id,
        clip_media_id=event.clip_media_id,
        details=EventDetails.model_validate(event.details) if event.details else None,
        occurred_at=event.occurred_at,
        received_at=event.received_at,
        reviewed=event.reviewed_at is not None,
    )


async def _one(db: AsyncSession, owner_id: uuid.UUID, event: Event) -> EventResponse:
    return _to_response(event, await service.people_of(db, owner_id, [event]))


# ---------- agent-facing ----------


@agent_router.post("")
async def report_events(
    data: AgentEventBatch, agent: CurrentAgent, db: DbSession
) -> AgentEventBatchResponse:
    """Report up to 100 events, oldest first. Each gets its own result; resend
    the batch after a lost reply and the events come back as duplicates."""
    return AgentEventBatchResponse(results=await service.record_events(db, agent, data))


@agent_router.patch("/{event_id}")
async def attach_media(
    event_id: uuid.UUID,
    data: AgentEventMediaRequest,
    agent: CurrentAgent,
    db: DbSession,
) -> AgentEventMediaResponse:
    """Attach a snapshot or clip registered after the event was reported."""
    try:
        event = await service.attach_media(db, agent, event_id, data)
    except service.EventError as exc:
        raise _http_error(exc) from None
    return AgentEventMediaResponse(
        event_id=event.id,
        snapshot_media_id=event.snapshot_media_id,
        clip_media_id=event.clip_media_id,
    )


# ---------- user-facing ----------


@user_router.get("")
async def list_events(
    user: CurrentUser,
    db: DbSession,
    camera_id: uuid.UUID | None = None,
    event_type: Annotated[EventType | None, Query(alias="type")] = None,
    person_id: uuid.UUID | None = None,
    severity: EventSeverity | None = None,
    unreviewed_only: bool = False,
    before: datetime | None = None,
    limit: Annotated[int, Query(ge=1, le=100)] = 50,
) -> list[EventResponse]:
    """Newest first. For the next page, pass the last item's occurred_at as before."""
    events = await service.list_events(
        db,
        user.id,
        camera_id=camera_id,
        event_type=event_type,
        person_id=person_id,
        severity=severity,
        unreviewed_only=unreviewed_only,
        before=before,
        limit=limit,
    )
    people = await service.people_of(db, user.id, events)
    return [_to_response(event, people) for event in events]


@user_router.get("/unreviewed-count")
async def unreviewed_count(user: CurrentUser, db: DbSession) -> UnreviewedCountResponse:
    return UnreviewedCountResponse(
        unreviewed=await service.count_unreviewed(db, user.id)
    )


@user_router.post("/review-all")
async def review_all(
    user: CurrentUser, db: DbSession, camera_id: uuid.UUID | None = None
) -> ReviewedAllResponse:
    """Mark every event as reviewed, or only those of one camera."""
    return ReviewedAllResponse(
        reviewed=await service.review_all(db, user.id, camera_id)
    )


@user_router.get("/{event_id}")
async def get_event(
    event_id: uuid.UUID, user: CurrentUser, db: DbSession
) -> EventResponse:
    try:
        event = await service.get_event(db, user.id, event_id)
    except service.EventError as exc:
        raise _http_error(exc) from None
    return await _one(db, user.id, event)


@user_router.patch("/{event_id}")
async def update_event(
    event_id: uuid.UUID, data: EventUpdateRequest, user: CurrentUser, db: DbSession
) -> EventResponse:
    try:
        event = await service.set_reviewed(db, user.id, event_id, data.reviewed)
    except service.EventError as exc:
        raise _http_error(exc) from None
    return await _one(db, user.id, event)


@user_router.delete("/{event_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_event(
    event_id: uuid.UUID, user: CurrentUser, db: DbSession
) -> Response:
    """Delete one event. Its snapshot and clip are kept; delete them under /media."""
    try:
        await service.delete_event(db, user.id, event_id)
    except service.EventError as exc:
        raise _http_error(exc) from None
    return Response(status_code=status.HTTP_204_NO_CONTENT)
