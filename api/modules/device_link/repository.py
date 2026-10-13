"""Database access for the device_link module."""

import uuid
from typing import Any

from sqlalchemy import delete, func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from api.modules.device_link.models import LinkedDevice


async def commit(db: AsyncSession) -> None:
    try:
        await db.commit()
    except Exception:
        await db.rollback()
        raise


async def add_device(db: AsyncSession, device: LinkedDevice) -> LinkedDevice:
    db.add(device)
    await commit(db)
    return device


async def get_device(
    db: AsyncSession, user_id: uuid.UUID, device_id: uuid.UUID
) -> LinkedDevice | None:
    return await db.scalar(
        select(LinkedDevice).where(
            LinkedDevice.id == device_id, LinkedDevice.user_id == user_id
        )
    )


async def get_by_refresh_hash(db: AsyncSession, token_hash: str) -> LinkedDevice | None:
    return await db.scalar(
        select(LinkedDevice).where(LinkedDevice.refresh_token_hash == token_hash)
    )


async def get_by_previous_hash(
    db: AsyncSession, token_hash: str
) -> LinkedDevice | None:
    return await db.scalar(
        select(LinkedDevice).where(LinkedDevice.previous_token_hash == token_hash)
    )


async def list_devices(db: AsyncSession, user_id: uuid.UUID) -> list[LinkedDevice]:
    result = await db.scalars(
        select(LinkedDevice)
        .where(LinkedDevice.user_id == user_id)
        .order_by(LinkedDevice.last_seen_at.desc(), LinkedDevice.id)
    )
    return list(result)


async def count_devices(db: AsyncSession, user_id: uuid.UUID) -> int:
    return await db.scalar(
        select(func.count())
        .select_from(LinkedDevice)
        .where(LinkedDevice.user_id == user_id)
    )


async def least_recently_used(
    db: AsyncSession, user_id: uuid.UUID, count: int
) -> list[uuid.UUID]:
    result = await db.scalars(
        select(LinkedDevice.id)
        .where(LinkedDevice.user_id == user_id)
        .order_by(LinkedDevice.last_seen_at, LinkedDevice.created_at)
        .limit(count)
    )
    return list(result)


async def rotate_refresh_token(
    db: AsyncSession, device_id: uuid.UUID, old_hash: str, values: dict[str, Any]
) -> bool:
    """Swap the refresh token, without committing.

    Returns False if another request already swapped `old_hash` away.
    """
    result = await db.execute(
        update(LinkedDevice)
        .where(
            LinkedDevice.id == device_id,
            LinkedDevice.refresh_token_hash == old_hash,
        )
        .values(previous_token_hash=old_hash, **values)
        .execution_options(synchronize_session="fetch")
    )
    return result.rowcount == 1


async def delete_devices(db: AsyncSession, device_ids: list[uuid.UUID]) -> int:
    if not device_ids:
        return 0
    result = await db.execute(
        delete(LinkedDevice)
        .where(LinkedDevice.id.in_(device_ids))
        .execution_options(synchronize_session="fetch")
    )
    await commit(db)
    return result.rowcount


async def delete_other_devices(
    db: AsyncSession, user_id: uuid.UUID, keep_id: uuid.UUID | None
) -> int:
    query = delete(LinkedDevice).where(LinkedDevice.user_id == user_id)
    if keep_id is not None:
        query = query.where(LinkedDevice.id != keep_id)
    result = await db.execute(query.execution_options(synchronize_session="fetch"))
    await commit(db)
    return result.rowcount


async def release_push_token(
    db: AsyncSession, provider: str, token: str, except_id: uuid.UUID | None = None
) -> None:
    """Take a push token off whichever device holds it, no commit."""
    query = update(LinkedDevice).where(
        LinkedDevice.push_provider == provider, LinkedDevice.push_token == token
    )
    if except_id is not None:
        query = query.where(LinkedDevice.id != except_id)
    await db.execute(
        query.values(push_provider=None, push_token=None).execution_options(
            synchronize_session="fetch"
        )
    )


async def list_push_targets(db: AsyncSession, user_id: uuid.UUID) -> list[LinkedDevice]:
    result = await db.scalars(
        select(LinkedDevice).where(
            LinkedDevice.user_id == user_id,
            LinkedDevice.notifications_enabled.is_(True),
            LinkedDevice.push_token.is_not(None),
        )
    )
    return list(result)
