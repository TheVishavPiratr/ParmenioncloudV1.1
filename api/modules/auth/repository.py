"""Database access for the auth module."""

import uuid
from datetime import datetime

from sqlalchemy import delete, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from api.modules.auth.models import OtpCode, User


async def commit(db: AsyncSession) -> None:
    try:
        await db.commit()
    except Exception:
        await db.rollback()
        raise


async def get_user_by_email(db: AsyncSession, email: str) -> User | None:
    return await db.scalar(select(User).where(User.email == email))


async def get_user_by_id(db: AsyncSession, user_id: uuid.UUID) -> User | None:
    return await db.get(User, user_id)


async def add_user(db: AsyncSession, user: User) -> User:
    """Insert a user and commit. Raises IntegrityError if the email is taken."""
    db.add(user)
    await commit(db)
    return user


async def delete_user(db: AsyncSession, user_id: uuid.UUID) -> None:
    """Delete a user; rows of other modules go with it (ON DELETE CASCADE)."""
    await db.execute(
        delete(User)
        .where(User.id == user_id)
        .execution_options(synchronize_session="fetch")
    )
    await commit(db)


async def get_active_otp(
    db: AsyncSession, user_id: uuid.UUID, purpose: str
) -> OtpCode | None:
    """Return the newest unused code for this user and purpose, if any."""
    return await db.scalar(
        select(OtpCode)
        .where(
            OtpCode.user_id == user_id,
            OtpCode.purpose == purpose,
            OtpCode.consumed_at.is_(None),
        )
        .order_by(OtpCode.created_at.desc())
        .limit(1)
    )


async def replace_otp(db: AsyncSession, otp: OtpCode) -> None:
    """Delete the user's unused codes for this purpose, store the new one, commit."""
    await db.execute(
        delete(OtpCode).where(
            OtpCode.user_id == otp.user_id,
            OtpCode.purpose == otp.purpose,
            OtpCode.consumed_at.is_(None),
        )
    )
    db.add(otp)
    await commit(db)


async def record_failed_otp_attempt(db: AsyncSession, otp_id: uuid.UUID) -> None:
    await db.execute(
        update(OtpCode)
        .where(OtpCode.id == otp_id)
        .values(attempts=OtpCode.attempts + 1)
    )
    await commit(db)


async def consume_otp(db: AsyncSession, otp_id: uuid.UUID, now: datetime) -> bool:
    """Mark a code as used without committing.

    Returns False if another request already used it.
    """
    result = await db.execute(
        update(OtpCode)
        .where(OtpCode.id == otp_id, OtpCode.consumed_at.is_(None))
        .values(consumed_at=now)
    )
    return result.rowcount == 1
