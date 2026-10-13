"""Database access for the events module."""

import uuid
from datetime import datetime

from sqlalchemy import delete, func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from api.modules.events.models import Event


async def commit(db: AsyncSession) -> None:
    try:
        await db.commit()
    except Exception:
        await db.rollback()
        raise


def stage(db: AsyncSession, event: Event) -> None:
    """Add a row to the session without committing."""
    db.add(event)


# ---------- agent side ----------


async def existing_client_ids(
    db: AsyncSession, agent_id: uuid.UUID, client_event_ids: list[str]
) -> dict[str, uuid.UUID]:
    """client_event_id -> event id, for the ones this agent already sent."""
    rows = await db.execute(
        select(Event.client_event_id, Event.id).where(
            Event.agent_id == agent_id, Event.client_event_id.in_(client_event_ids)
        )
    )
    return {row.client_event_id: row.id for row in rows}


async def get_event_for_agent(
    db: AsyncSession, agent_id: uuid.UUID, event_id: uuid.UUID
) -> Event | None:
    return await db.scalar(
        select(Event).where(Event.id == event_id, Event.agent_id == agent_id)
    )


async def has_recent_alert(
    db: AsyncSession,
    camera_id: uuid.UUID,
    event_type: str,
    person_id: uuid.UUID | None,
    since: datetime,
) -> bool:
    """True if the same thing on the same camera caused an alert after `since`."""
    query = select(Event.id).where(
        Event.camera_id == camera_id,
        Event.type == event_type,
        Event.occurred_at > since,
        Event.notified.is_(True),
    )
    if person_id is None:
        query = query.where(Event.person_id.is_(None))
    else:
        query = query.where(Event.person_id == person_id)
    return await db.scalar(query.limit(1)) is not None


async def mark_notified(db: AsyncSession, event_ids: list[uuid.UUID]) -> None:
    await db.execute(
        update(Event)
        .where(Event.id.in_(event_ids))
        .values(notified=True)
        .execution_options(synchronize_session="fetch")
    )
    await commit(db)


# ---------- user side ----------


async def get_event(
    db: AsyncSession, owner_id: uuid.UUID, event_id: uuid.UUID
) -> Event | None:
    return await db.scalar(
        select(Event).where(Event.id == event_id, Event.owner_id == owner_id)
    )


async def list_events(
    db: AsyncSession,
    owner_id: uuid.UUID,
    *,
    camera_id: uuid.UUID | None,
    event_type: str | None,
    person_id: uuid.UUID | None,
    severity: str | None,
    unreviewed_only: bool,
    before: datetime | None,
    limit: int,
) -> list[Event]:
    """Newest first. Pass the last item's occurred_at as `before` for the next page."""
    query = select(Event).where(Event.owner_id == owner_id)
    if camera_id is not None:
        query = query.where(Event.camera_id == camera_id)
    if event_type is not None:
        query = query.where(Event.type == event_type)
    if person_id is not None:
        query = query.where(Event.person_id == person_id)
    if severity is not None:
        query = query.where(Event.severity == severity)
    if unreviewed_only:
        query = query.where(Event.reviewed_at.is_(None))
    if before is not None:
        query = query.where(Event.occurred_at < before)
    result = await db.scalars(
        query.order_by(Event.occurred_at.desc(), Event.id).limit(limit)
    )
    return list(result)


async def count_unreviewed(db: AsyncSession, owner_id: uuid.UUID) -> int:
    return await db.scalar(
        select(func.count(Event.id)).where(
            Event.owner_id == owner_id, Event.reviewed_at.is_(None)
        )
    )


async def review_all(
    db: AsyncSession, owner_id: uuid.UUID, camera_id: uuid.UUID | None, now: datetime
) -> int:
    query = update(Event).where(Event.owner_id == owner_id, Event.reviewed_at.is_(None))
    if camera_id is not None:
        query = query.where(Event.camera_id == camera_id)
    result = await db.execute(
        query.values(reviewed_at=now).execution_options(synchronize_session="fetch")
    )
    await commit(db)
    return result.rowcount


async def delete_event(db: AsyncSession, event_id: uuid.UUID) -> None:
    await db.execute(
        delete(Event)
        .where(Event.id == event_id)
        .execution_options(synchronize_session="fetch")
    )
    await commit(db)


async def delete_older_than(db: AsyncSession, cutoff: datetime, limit: int) -> int:
    """Delete one batch of events that happened before `cutoff`. Returns how many."""
    ids = list(
        await db.scalars(
            select(Event.id).where(Event.occurred_at < cutoff).limit(limit)
        )
    )
    if ids:
        await db.execute(
            delete(Event)
            .where(Event.id.in_(ids))
            .execution_options(synchronize_session="fetch")
        )
    await commit(db)
    return len(ids)
