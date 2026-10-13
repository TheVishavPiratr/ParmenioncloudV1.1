"""Database access for the audit module. Entries are only added, listed and
purged after the retention period; nothing updates them."""

import uuid
from datetime import datetime

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from api.modules.audit.models import AuditEntry


async def commit(db: AsyncSession) -> None:
    try:
        await db.commit()
    except Exception:
        await db.rollback()
        raise


async def add_entry(db: AsyncSession, entry: AuditEntry) -> None:
    """Write the entry inside a savepoint, so a failed write rolls back only
    the entry and leaves the caller's loaded objects usable."""
    async with db.begin_nested():
        db.add(entry)
    await commit(db)


async def has_recent_entry(
    db: AsyncSession,
    user_id: uuid.UUID | None,
    action: str,
    target_id: uuid.UUID | None,
    since: datetime,
) -> bool:
    query = select(AuditEntry.id).where(
        AuditEntry.user_id == user_id,
        AuditEntry.action == action,
        AuditEntry.target_id == target_id,
        AuditEntry.created_at >= since,
    )
    return await db.scalar(query.limit(1)) is not None


async def list_entries(
    db: AsyncSession,
    user_id: uuid.UUID,
    *,
    action: str | None,
    before: datetime | None,
    limit: int,
) -> list[AuditEntry]:
    """Newest first. Pass the last item's created_at as `before` for the next page."""
    query = select(AuditEntry).where(AuditEntry.user_id == user_id)
    if action is not None:
        query = query.where(AuditEntry.action == action)
    if before is not None:
        query = query.where(AuditEntry.created_at < before)
    result = await db.scalars(
        query.order_by(AuditEntry.created_at.desc(), AuditEntry.id).limit(limit)
    )
    return list(result)


async def delete_entries_of_user(db: AsyncSession, user_id: uuid.UUID) -> int:
    result = await db.execute(
        delete(AuditEntry)
        .where(AuditEntry.user_id == user_id)
        .execution_options(synchronize_session="fetch")
    )
    await commit(db)
    return result.rowcount


async def delete_older_than(db: AsyncSession, cutoff: datetime, limit: int) -> int:
    """Delete one batch of entries created before `cutoff`. Returns how many."""
    ids = list(
        await db.scalars(
            select(AuditEntry.id).where(AuditEntry.created_at < cutoff).limit(limit)
        )
    )
    if ids:
        await db.execute(
            delete(AuditEntry)
            .where(AuditEntry.id.in_(ids))
            .execution_options(synchronize_session="fetch")
        )
    await commit(db)
    return len(ids)
