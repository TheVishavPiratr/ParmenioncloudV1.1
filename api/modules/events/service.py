"""Business logic for the events module.

Edge agents report what their cameras saw in batches. The agent uploads the
snapshot through media_access first and passes its media id with the event;
a clip recorded afterwards is attached with a second call. The cloud checks
every reference, decides the severity itself, and alerts the owner through
notifications, at most once per cooldown for the same thing on the same
camera. Backlog from an agent that was offline is stored without alerting.

Nothing in other modules calls into events.
"""

import asyncio
import logging
import uuid
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any

from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from api.core.config import settings
from api.core.database import SessionLocal
from api.modules.audit import service as audit_service
from api.modules.cameras import service as cameras_service
from api.modules.events import repository
from api.modules.events.models import Event, EventSeverity, EventType
from api.modules.events.schemas import (
    AgentEvent,
    AgentEventBatch,
    AgentEventMediaRequest,
    AgentEventResult,
)
from api.modules.gallery import service as gallery_service
from api.modules.media_access import service as media_service
from api.modules.notifications import service as notifications_service

if TYPE_CHECKING:
    from api.modules.cameras.models import Camera
    from api.modules.edge_agent.models import EdgeAgent
    from api.modules.gallery.models import Person

logger = logging.getLogger(__name__)

AuditAction = audit_service.AuditAction
MediaKind = media_service.MediaKind

# No second alert for the same thing on the same camera within this time.
ALERT_COOLDOWN = timedelta(seconds=60)
CRITICAL_ALERT_COOLDOWN = timedelta(seconds=15)
# Older events are backlog from an agent that was offline: stored, not alerted.
ALERT_MAX_AGE = timedelta(minutes=10)
PURGE_INTERVAL = timedelta(hours=6)
PURGE_BATCH = 1000

_ALERTS: dict[EventType, tuple[str, str]] = {
    EventType.MOTION: ("Motion detected", "Motion on {camera}."),
    EventType.PERSON_DETECTED: ("Person detected", "A person was seen on {camera}."),
    EventType.FACE_UNKNOWN: (
        "Unknown face",
        "Someone not in your gallery was seen on {camera}.",
    ),
    EventType.CAMERA_TAMPERED: (
        "Camera tampering",
        "{camera} may have been covered or moved.",
    ),
}


class EventError(Exception):
    """Base of every error this service raises."""


class EventNotFoundError(EventError):
    pass


class MediaNotFoundError(EventError):
    pass


class MediaAlreadyAttachedError(EventError):
    pass


def _utcnow() -> datetime:
    return datetime.now(UTC)


def _as_utc(value: datetime) -> datetime:
    # Some databases return naive datetimes; every stored time is UTC.
    return value if value.tzinfo else value.replace(tzinfo=UTC)


def severity_of(event_type: str, person: "Person | None") -> EventSeverity:
    if event_type == EventType.FACE_RECOGNIZED:
        if (
            person is not None
            and person.category == gallery_service.PersonCategory.BLOCKED
        ):
            return EventSeverity.CRITICAL
        return EventSeverity.INFO
    if event_type == EventType.MOTION:
        return EventSeverity.INFO
    return EventSeverity.WARNING


async def _media_fits(
    db: AsyncSession,
    agent_id: uuid.UUID,
    camera_id: uuid.UUID | None,
    media_id: uuid.UUID,
    kind: MediaKind,
) -> bool:
    """True if the agent registered this file, from this camera, of this kind."""
    if camera_id is None:
        return False
    try:
        media = await media_service.get_agent_media(db, agent_id, media_id)
    except media_service.MediaNotFoundError:
        return False
    return media.camera_id == camera_id and media.kind == kind


# ---------- agent actions ----------


async def record_events(
    db: AsyncSession, agent: "EdgeAgent", batch: AgentEventBatch
) -> list[AgentEventResult]:
    """Store a batch of events, one result per event in the order sent.

    Resending an event (same client_event_id) is reported as a duplicate with
    the first event's id, so an agent can retry a whole batch after a lost
    reply. A bad reference rejects only that event, never the batch.
    """
    agent_id = agent.id
    owner_id = agent.owner_id
    try:
        results, created, cameras, people = await _ingest(
            db, agent_id, owner_id, batch.events
        )
    except IntegrityError:
        # The same batch raced in from a retry (commit rolled back); this
        # pass sees the winner's events and reports them as duplicates.
        results, created, cameras, people = await _ingest(
            db, agent_id, owner_id, batch.events
        )
    await _send_alerts(db, created, cameras, people)
    return results


async def _ingest(
    db: AsyncSession,
    agent_id: uuid.UUID,
    owner_id: uuid.UUID,
    events: list[AgentEvent],
) -> tuple[
    list[AgentEventResult],
    list[Event],
    dict[uuid.UUID, "Camera"],
    dict[uuid.UUID, "Person"],
]:
    now = _utcnow()
    oldest = now - timedelta(days=settings.event_retention_days)
    seen = await repository.existing_client_ids(
        db, agent_id, [e.client_event_id for e in events]
    )
    people = await gallery_service.get_people(
        db, owner_id, list({e.person_id for e in events if e.person_id is not None})
    )
    cameras: dict[uuid.UUID, Camera | None] = {}
    results: list[AgentEventResult] = []
    created: list[Event] = []

    def rejected(item: AgentEvent, reason: str) -> AgentEventResult:
        return AgentEventResult(
            client_event_id=item.client_event_id,
            status="rejected",
            event_id=None,
            reason=reason,
        )

    for item in events:
        if item.client_event_id in seen:
            results.append(
                AgentEventResult(
                    client_event_id=item.client_event_id,
                    status="duplicate",
                    event_id=seen[item.client_event_id],
                )
            )
            continue
        if item.occurred_at < oldest:
            results.append(rejected(item, "too_old"))
            continue
        if item.camera_id not in cameras:
            try:
                cameras[item.camera_id] = await cameras_service.get_agent_camera(
                    db, agent_id, item.camera_id
                )
            except cameras_service.CameraNotFoundError:
                cameras[item.camera_id] = None
        camera = cameras[item.camera_id]
        if camera is None:
            results.append(rejected(item, "camera_not_found"))
            continue
        media_ok = True
        for media_id, kind in (
            (item.snapshot_media_id, MediaKind.SNAPSHOT),
            (item.clip_media_id, MediaKind.CLIP),
        ):
            if media_id is not None and not await _media_fits(
                db, agent_id, camera.id, media_id, kind
            ):
                media_ok = False
        if not media_ok:
            results.append(rejected(item, "media_not_found"))
            continue

        # A person removed from the gallery since the match is not an error:
        # the event is kept, without saying who it was.
        person = people.get(item.person_id) if item.person_id else None
        event = Event(
            id=uuid.uuid4(),
            owner_id=camera.owner_id,
            agent_id=agent_id,
            camera_id=camera.id,
            client_event_id=item.client_event_id,
            type=item.type,
            severity=severity_of(item.type, person),
            person_id=person.id if person else None,
            confidence=item.confidence,
            face_model=item.face_model,
            snapshot_media_id=item.snapshot_media_id,
            clip_media_id=item.clip_media_id,
            details=item.details.model_dump(exclude_none=True)
            if item.details
            else None,
            occurred_at=item.occurred_at,
            received_at=now,
            notified=False,
        )
        repository.stage(db, event)
        seen[item.client_event_id] = event.id
        created.append(event)
        results.append(
            AgentEventResult(
                client_event_id=item.client_event_id,
                status="created",
                event_id=event.id,
            )
        )

    await repository.commit(db)
    known_cameras = {c.id: c for c in cameras.values() if c is not None}
    return results, created, known_cameras, people


async def attach_media(
    db: AsyncSession,
    agent: "EdgeAgent",
    event_id: uuid.UUID,
    data: AgentEventMediaRequest,
) -> Event:
    """Fill in an event's snapshot or clip. Attaching the same file again is a
    no-op; replacing a different one is refused."""
    event = await repository.get_event_for_agent(db, agent.id, event_id)
    if event is None:
        raise EventNotFoundError
    changes: dict[str, uuid.UUID] = {}
    for field, media_id, kind in (
        ("snapshot_media_id", data.snapshot_media_id, MediaKind.SNAPSHOT),
        ("clip_media_id", data.clip_media_id, MediaKind.CLIP),
    ):
        current = getattr(event, field)
        if media_id is None or current == media_id:
            continue
        if current is not None:
            raise MediaAlreadyAttachedError
        if not await _media_fits(db, agent.id, event.camera_id, media_id, kind):
            raise MediaNotFoundError
        changes[field] = media_id
    if changes:
        for field, media_id in changes.items():
            setattr(event, field, media_id)
        await repository.commit(db)
    return event


# ---------- alerts ----------


def _alert_text(
    event: Event, camera: "Camera", person: "Person | None"
) -> tuple[str, str, str]:
    """(kind, title, body) of the alert for an event."""
    camera_name = camera.name or "a camera"
    if event.type == EventType.FACE_RECOGNIZED:
        name = person.name if person else "Someone from your gallery"
        if event.severity == EventSeverity.CRITICAL:
            return (
                "blocked_person_seen",
                "Blocked person seen",
                f"{name} was seen on {camera_name}.",
            )
        return "person_recognized", f"{name} seen", f"{name} was seen on {camera_name}."
    title, body = _ALERTS[EventType(event.type)]
    return event.type, title, body.format(camera=camera_name)


async def _send_alerts(
    db: AsyncSession,
    events: list[Event],
    cameras: dict[uuid.UUID, "Camera"],
    people: dict[uuid.UUID, "Person"],
) -> None:
    """Alert the owner about fresh events, skipping repeats within the cooldown."""
    now = _utcnow()
    due: list[Event] = []
    last_alert: dict[tuple[Any, ...], datetime] = {}
    for event in sorted(events, key=lambda e: e.occurred_at):
        occurred = _as_utc(event.occurred_at)
        if occurred < now - ALERT_MAX_AGE:
            continue
        cooldown = (
            CRITICAL_ALERT_COOLDOWN
            if event.severity == EventSeverity.CRITICAL
            else ALERT_COOLDOWN
        )
        key = (event.camera_id, event.type, event.person_id)
        previous = last_alert.get(key)
        if previous is not None:
            if occurred - previous < cooldown:
                continue
        elif await repository.has_recent_alert(
            db, event.camera_id, event.type, event.person_id, occurred - cooldown
        ):
            continue
        last_alert[key] = occurred
        due.append(event)
    if not due:
        return

    await repository.mark_notified(db, [event.id for event in due])
    for event in due:
        kind, title, body = _alert_text(
            event, cameras[event.camera_id], people.get(event.person_id)
        )
        await notifications_service.notify(
            db,
            event.owner_id,
            notifications_service.NotificationCategory.DETECTION,
            kind,
            title,
            body,
            {
                "event_id": str(event.id),
                "camera_id": str(event.camera_id),
                "type": event.type,
                "severity": event.severity,
            },
        )


# ---------- user actions ----------


async def list_events(
    db: AsyncSession,
    owner_id: uuid.UUID,
    *,
    camera_id: uuid.UUID | None = None,
    event_type: EventType | None = None,
    person_id: uuid.UUID | None = None,
    severity: EventSeverity | None = None,
    unreviewed_only: bool = False,
    before: datetime | None = None,
    limit: int = 50,
) -> list[Event]:
    return await repository.list_events(
        db,
        owner_id,
        camera_id=camera_id,
        event_type=event_type,
        person_id=person_id,
        severity=severity,
        unreviewed_only=unreviewed_only,
        before=before,
        limit=limit,
    )


async def people_of(
    db: AsyncSession, owner_id: uuid.UUID, events: list[Event]
) -> dict[uuid.UUID, "Person"]:
    """The gallery people the events refer to, by id."""
    ids = list({e.person_id for e in events if e.person_id is not None})
    return await gallery_service.get_people(db, owner_id, ids)


async def get_event(
    db: AsyncSession, owner_id: uuid.UUID, event_id: uuid.UUID
) -> Event:
    event = await repository.get_event(db, owner_id, event_id)
    if event is None:
        raise EventNotFoundError
    return event


async def set_reviewed(
    db: AsyncSession, owner_id: uuid.UUID, event_id: uuid.UUID, reviewed: bool
) -> Event:
    event = await get_event(db, owner_id, event_id)
    if reviewed and event.reviewed_at is None:
        event.reviewed_at = _utcnow()
    elif not reviewed:
        event.reviewed_at = None
    await repository.commit(db)
    return event


async def review_all(
    db: AsyncSession, owner_id: uuid.UUID, camera_id: uuid.UUID | None = None
) -> int:
    return await repository.review_all(db, owner_id, camera_id, _utcnow())


async def count_unreviewed(db: AsyncSession, owner_id: uuid.UUID) -> int:
    return await repository.count_unreviewed(db, owner_id)


async def delete_event(
    db: AsyncSession, owner_id: uuid.UUID, event_id: uuid.UUID
) -> None:
    """Delete one event. Its snapshot and clip stay in media until retention."""
    event = await get_event(db, owner_id, event_id)
    details = {"type": event.type, "camera_id": str(event.camera_id)}
    await repository.delete_event(db, event.id)
    await audit_service.record(
        db,
        AuditAction.EVENT_DELETED,
        user_id=owner_id,
        target_type="event",
        target_id=event_id,
        details=details,
    )


# ---------- retention ----------


async def purge_expired(db: AsyncSession, now: datetime | None = None) -> int:
    """Delete events older than the retention period. Returns how many."""
    cutoff = (now or _utcnow()) - timedelta(days=settings.event_retention_days)
    removed = 0
    while count := await repository.delete_older_than(db, cutoff, PURGE_BATCH):
        removed += count
    return removed


async def run_retention_loop() -> None:
    """Purge forever, once per PURGE_INTERVAL. Started by the app on startup."""
    while True:
        try:
            async with SessionLocal() as db:
                removed = await purge_expired(db)
            if removed:
                logger.info("Event retention removed %d events", removed)
        except Exception:
            logger.exception("Event retention run failed")
        await asyncio.sleep(PURGE_INTERVAL.total_seconds())
