"""Business logic for the audit module.

Other modules may call: record, forget_user, AuditAction, AuditActor. Call record after
the action has been committed (or, for a refused attempt, before raising), so
the log only says something happened when it did. record commits the session,
so never call it with uncommitted changes pending.
"""

import asyncio
import logging
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from api.core.config import settings
from api.core.database import SessionLocal
from api.modules.audit import context, repository
from api.modules.audit.models import AuditAction, AuditActor, AuditEntry

PURGE_INTERVAL = timedelta(hours=6)
PURGE_BATCH = 1000

logger = logging.getLogger(__name__)


def _utcnow() -> datetime:
    return datetime.now(UTC)


async def record(
    db: AsyncSession,
    action: AuditAction,
    *,
    user_id: uuid.UUID | None,
    success: bool = True,
    actor: AuditActor | None = None,
    agent_id: uuid.UUID | None = None,
    target_type: str | None = None,
    target_id: uuid.UUID | None = None,
    details: dict[str, Any] | None = None,
    dedupe_within: timedelta | None = None,
) -> None:
    """Add an entry, with the IP and user agent of the current request.

    The actor defaults to "agent" when agent_id is given, else "user". With
    dedupe_within, nothing is added if the same user already has this action
    on the same target that recently (e.g. a viewer re-opening a photo). A
    failure to write is logged, never raised: the action itself has already
    happened, and the caller must still get its answer.
    """
    now = _utcnow()
    if dedupe_within is not None and await repository.has_recent_entry(
        db, user_id, action, target_id, now - dedupe_within
    ):
        return
    if actor is None:
        actor = AuditActor.AGENT if agent_id is not None else AuditActor.USER
    request = context.current()
    entry = AuditEntry(
        user_id=user_id,
        actor=actor,
        agent_id=agent_id,
        action=action,
        success=success,
        target_type=target_type,
        target_id=target_id,
        details=details,
        ip=request.ip,
        user_agent=request.user_agent,
        created_at=now,
    )
    try:
        await repository.add_entry(db, entry)
    except Exception:
        logger.exception("Could not write audit entry %s", action)


async def forget_user(db: AsyncSession, user_id: uuid.UUID) -> int:
    """Delete every entry about a user whose account is being deleted: they
    hold the user's IP addresses and devices. Returns how many."""
    return await repository.delete_entries_of_user(db, user_id)


async def list_entries(
    db: AsyncSession,
    user_id: uuid.UUID,
    *,
    action: AuditAction | None = None,
    before: datetime | None = None,
    limit: int = 50,
) -> list[AuditEntry]:
    return await repository.list_entries(
        db, user_id, action=action, before=before, limit=limit
    )


# ---------- retention ----------


async def purge_expired(db: AsyncSession, now: datetime | None = None) -> int:
    """Delete entries older than the retention period. Returns how many."""
    cutoff = (now or _utcnow()) - timedelta(days=settings.audit_retention_days)
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
                logger.info("Audit retention removed %d entries", removed)
        except Exception:
            logger.exception("Audit retention run failed")
        await asyncio.sleep(PURGE_INTERVAL.total_seconds())
