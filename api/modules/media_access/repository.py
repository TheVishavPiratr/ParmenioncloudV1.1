"""Database access for the media_access module."""

import uuid
from datetime import datetime

from sqlalchemy import delete, func, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from api.modules.media_access.models import Media, MediaStatus


async def commit(db: AsyncSession) -> None:
    try:
        await db.commit()
    except Exception:
        await db.rollback()
        raise


async def add_media(db: AsyncSession, media: Media) -> Media:
    db.add(media)
    await commit(db)
    return media


async def get_by_hash(
    db: AsyncSession, camera_id: uuid.UUID, sha256: str
) -> Media | None:
    return await db.scalar(
        select(Media).where(Media.camera_id == camera_id, Media.sha256 == sha256)
    )


async def get_media_for_agent(
    db: AsyncSession, agent_id: uuid.UUID, media_id: uuid.UUID
) -> Media | None:
    return await db.scalar(
        select(Media).where(Media.id == media_id, Media.agent_id == agent_id)
    )


async def get_ready_media(
    db: AsyncSession, media_id: uuid.UUID, owner_id: uuid.UUID | None = None
) -> Media | None:
    query = select(Media).where(Media.id == media_id, Media.status == MediaStatus.READY)
    if owner_id is not None:
        query = query.where(Media.owner_id == owner_id)
    return await db.scalar(query)


async def list_ready_media(
    db: AsyncSession,
    owner_id: uuid.UUID,
    *,
    camera_id: uuid.UUID | None,
    kind: str | None,
    before: datetime | None,
    limit: int,
) -> list[Media]:
    """Newest first. Pass the last item's captured_at as `before` for the next page."""
    query = select(Media).where(
        Media.owner_id == owner_id, Media.status == MediaStatus.READY
    )
    if camera_id is not None:
        query = query.where(Media.camera_id == camera_id)
    if kind is not None:
        query = query.where(Media.kind == kind)
    if before is not None:
        query = query.where(Media.captured_at < before)
    result = await db.scalars(
        query.order_by(Media.captured_at.desc(), Media.id).limit(limit)
    )
    return list(result)


async def count_pending(db: AsyncSession, agent_id: uuid.UUID) -> int:
    return await db.scalar(
        select(func.count())
        .select_from(Media)
        .where(Media.agent_id == agent_id, Media.status == MediaStatus.PENDING)
    )


async def mark_ready(db: AsyncSession, media_id: uuid.UUID, now: datetime) -> None:
    await db.execute(
        update(Media)
        .where(Media.id == media_id, Media.status == MediaStatus.PENDING)
        .values(status=MediaStatus.READY, uploaded_at=now)
        .execution_options(synchronize_session="fetch")
    )
    await commit(db)


async def delete_media(db: AsyncSession, media_id: uuid.UUID) -> None:
    await db.execute(
        delete(Media)
        .where(Media.id == media_id)
        .execution_options(synchronize_session="fetch")
    )
    await commit(db)


async def take_owner_media(db: AsyncSession, owner_id: uuid.UUID) -> list[str]:
    """Delete every media row of the owner and return their storage keys."""
    keys = list(
        await db.scalars(select(Media.storage_key).where(Media.owner_id == owner_id))
    )
    await db.execute(
        delete(Media)
        .where(Media.owner_id == owner_id)
        .execution_options(synchronize_session="fetch")
    )
    await commit(db)
    return keys


async def take_expired(
    db: AsyncSession, captured_before: datetime, pending_before: datetime, limit: int
) -> list[str]:
    """Delete one batch of expired rows and return their storage keys.

    Expired: ready media captured before `captured_before`, and uploads that
    were registered before `pending_before` but never finished.
    """
    expired = or_(
        Media.captured_at < captured_before,
        (Media.status == MediaStatus.PENDING) & (Media.created_at < pending_before),
    )
    rows = (
        await db.execute(
            select(Media.id, Media.storage_key).where(expired).limit(limit)
        )
    ).all()
    if rows:
        await db.execute(
            delete(Media)
            .where(Media.id.in_([row.id for row in rows]))
            .execution_options(synchronize_session="fetch")
        )
    await commit(db)
    return [row.storage_key for row in rows]
