"""Database access for the notifications module."""

import uuid
from datetime import datetime

from sqlalchemy import delete, func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from api.modules.notifications.models import Notification, NotificationPreference


async def commit(db: AsyncSession) -> None:
    try:
        await db.commit()
    except Exception:
        await db.rollback()
        raise


async def add_notification(db: AsyncSession, notification: Notification) -> None:
    """Write inside a savepoint, so a failed write rolls back only the
    notification and leaves the caller's loaded objects usable."""
    async with db.begin_nested():
        db.add(notification)
    await commit(db)


async def list_notifications(
    db: AsyncSession,
    user_id: uuid.UUID,
    *,
    unread_only: bool,
    before: datetime | None,
    limit: int,
) -> list[Notification]:
    """Newest first. Pass the last item's created_at as `before` for the next page."""
    query = select(Notification).where(Notification.user_id == user_id)
    if unread_only:
        query = query.where(Notification.read_at.is_(None))
    if before is not None:
        query = query.where(Notification.created_at < before)
    result = await db.scalars(
        query.order_by(Notification.created_at.desc(), Notification.id).limit(limit)
    )
    return list(result)


async def count_unread(db: AsyncSession, user_id: uuid.UUID) -> int:
    return await db.scalar(
        select(func.count(Notification.id)).where(
            Notification.user_id == user_id, Notification.read_at.is_(None)
        )
    )


async def get_notification(
    db: AsyncSession, user_id: uuid.UUID, notification_id: uuid.UUID
) -> Notification | None:
    return await db.scalar(
        select(Notification).where(
            Notification.id == notification_id, Notification.user_id == user_id
        )
    )


async def mark_all_read(db: AsyncSession, user_id: uuid.UUID, now: datetime) -> int:
    result = await db.execute(
        update(Notification)
        .where(Notification.user_id == user_id, Notification.read_at.is_(None))
        .values(read_at=now)
        .execution_options(synchronize_session="fetch")
    )
    await commit(db)
    return result.rowcount


async def delete_notification(db: AsyncSession, notification_id: uuid.UUID) -> None:
    await db.execute(
        delete(Notification)
        .where(Notification.id == notification_id)
        .execution_options(synchronize_session="fetch")
    )
    await commit(db)


async def delete_older_than(db: AsyncSession, cutoff: datetime, limit: int) -> int:
    """Delete one batch of notifications created before `cutoff`. Returns how many."""
    ids = list(
        await db.scalars(
            select(Notification.id).where(Notification.created_at < cutoff).limit(limit)
        )
    )
    if ids:
        await db.execute(
            delete(Notification)
            .where(Notification.id.in_(ids))
            .execution_options(synchronize_session="fetch")
        )
    await commit(db)
    return len(ids)


# ---------- preferences ----------


async def list_preferences(
    db: AsyncSession, user_id: uuid.UUID
) -> list[NotificationPreference]:
    result = await db.scalars(
        select(NotificationPreference).where(NotificationPreference.user_id == user_id)
    )
    return list(result)


async def get_preference(
    db: AsyncSession, user_id: uuid.UUID, category: str
) -> NotificationPreference | None:
    return await db.get(NotificationPreference, (user_id, category))


def stage(db: AsyncSession, preference: NotificationPreference) -> None:
    db.add(preference)
