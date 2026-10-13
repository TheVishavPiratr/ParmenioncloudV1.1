"""Business logic for the media_access module.

The edge agent's only job is one upload per file: register it, then PUT the
bytes. The cloud does the rest: checking the file, storing it, deciding who
may see it, serving it (with seeking), and deleting it after the retention
period.

Other modules may call: get_owned_media, get_agent_media, create_link,
delete_all_for_owner.
"""

import asyncio
import logging
import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING

from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from api.core import signed_links, storage
from api.core.config import settings
from api.core.database import SessionLocal
from api.modules.audit import service as audit_service
from api.modules.cameras import service as cameras_service
from api.modules.media_access import repository
from api.modules.media_access.models import Media, MediaKind, MediaStatus
from api.modules.media_access.schemas import MediaCreateRequest

if TYPE_CHECKING:
    from api.modules.edge_agent.models import EdgeAgent

logger = logging.getLogger(__name__)

AuditAction = audit_service.AuditAction

# Registered but not yet uploaded files per agent.
MAX_PENDING_PER_AGENT = 100
# An upload not finished within this time is given up and cleaned away.
PENDING_TTL = timedelta(hours=24)
LINK_TTL = timedelta(minutes=10)
LINK_PURPOSE = "media"
PURGE_INTERVAL = timedelta(hours=1)
PURGE_BATCH = 500

_EXTENSIONS = {
    "image/jpeg": ".jpg",
    "image/png": ".png",
    "image/webp": ".webp",
    "video/mp4": ".mp4",
    "video/webm": ".webm",
}


class MediaError(Exception):
    """Base of every error this service raises."""


class MediaNotFoundError(MediaError):
    pass


class CameraNotFoundError(MediaError):
    pass


class UploadLimitError(MediaError):
    pass


class UploadTooLargeError(MediaError):
    pass


class UploadMismatchError(MediaError):
    pass


class InvalidLinkError(MediaError):
    pass


def _utcnow() -> datetime:
    return datetime.now(UTC)


def _storage_key(media_id: uuid.UUID, content_type: str) -> str:
    # Two-character folders keep any one directory from growing too large.
    return f"{media_id.hex[:2]}/{media_id.hex}{_EXTENSIONS[content_type]}"


# ---------- agent actions ----------


async def register_upload(
    db: AsyncSession, agent: "EdgeAgent", data: MediaCreateRequest
) -> Media:
    """Record a file the agent is about to upload.

    Registering the same file again (same camera and hash) returns the first
    record, so an agent can simply retry after a lost reply.
    """
    try:
        camera = await cameras_service.get_agent_camera(db, agent.id, data.camera_id)
    except cameras_service.CameraNotFoundError:
        raise CameraNotFoundError from None
    existing = await repository.get_by_hash(db, camera.id, data.sha256)
    if existing is not None:
        return existing
    if await repository.count_pending(db, agent.id) >= MAX_PENDING_PER_AGENT:
        raise UploadLimitError

    media_id = uuid.uuid4()
    media = Media(
        id=media_id,
        owner_id=camera.owner_id,
        agent_id=agent.id,
        camera_id=camera.id,
        kind=data.kind,
        content_type=data.content_type,
        status=MediaStatus.PENDING,
        size_bytes=data.size_bytes,
        sha256=data.sha256,
        storage_key=_storage_key(media_id, data.content_type),
        duration_s=data.duration_s,
        captured_at=data.captured_at,
        created_at=_utcnow(),
    )
    try:
        return await repository.add_media(db, media)
    except IntegrityError:
        # The same registration raced in from a retry; use the winner.
        existing = await repository.get_by_hash(db, camera.id, data.sha256)
        if existing is None:
            raise
        return existing


async def upload_content(
    db: AsyncSession,
    agent: "EdgeAgent",
    media_id: uuid.UUID,
    chunks: AsyncIterator[bytes],
) -> Media:
    """Store the bytes of a registered file. Uploading a finished one again is
    a no-op, so retries are safe."""
    media = await repository.get_media_for_agent(db, agent.id, media_id)
    if media is None:
        raise MediaNotFoundError
    if media.status == MediaStatus.READY:
        return media
    # Give the database connection back while the bytes stream in: a large
    # clip on a slow uplink must not hold a pool connection for minutes.
    await repository.commit(db)
    try:
        await storage.save(
            media.storage_key,
            chunks,
            max_bytes=media.size_bytes,
            size=media.size_bytes,
            sha256=media.sha256,
        )
    except storage.TooLargeError:
        raise UploadTooLargeError from None
    except storage.MismatchError:
        raise UploadMismatchError from None
    await repository.mark_ready(db, media.id, _utcnow())
    media.status = MediaStatus.READY
    return media


async def get_agent_media(
    db: AsyncSession, agent_id: uuid.UUID, media_id: uuid.UUID
) -> Media:
    """A file this agent registered, uploaded yet or not."""
    media = await repository.get_media_for_agent(db, agent_id, media_id)
    if media is None:
        raise MediaNotFoundError
    return media


# ---------- user actions ----------


async def list_media(
    db: AsyncSession,
    owner_id: uuid.UUID,
    *,
    camera_id: uuid.UUID | None = None,
    kind: MediaKind | None = None,
    before: datetime | None = None,
    limit: int = 50,
) -> list[Media]:
    return await repository.list_ready_media(
        db, owner_id, camera_id=camera_id, kind=kind, before=before, limit=limit
    )


async def get_owned_media(
    db: AsyncSession, owner_id: uuid.UUID, media_id: uuid.UUID
) -> Media:
    media = await repository.get_ready_media(db, media_id, owner_id)
    if media is None:
        raise MediaNotFoundError
    return media


async def create_link(
    db: AsyncSession, owner_id: uuid.UUID, media_id: uuid.UUID
) -> tuple[str, datetime]:
    """A short-lived signed URL for viewing the file. Returns (url, expires_at)."""
    media = await get_owned_media(db, owner_id, media_id)
    link = signed_links.create(
        LINK_PURPOSE, f"/media/files/{media.id}", media.id, LINK_TTL
    )
    await audit_service.record(
        db,
        AuditAction.MEDIA_LINK_CREATED,
        user_id=owner_id,
        target_type="media",
        target_id=media.id,
        # One entry per viewing, not one per scrub or reload.
        dedupe_within=LINK_TTL,
    )
    return link


async def open_link(
    db: AsyncSession, media_id: uuid.UUID, expires: int, signature: str
) -> tuple[Path, str]:
    """Check a signed URL. Returns (file path, content type).

    The signature is checked before touching the database, so forged links
    cost almost nothing.
    """
    if not signed_links.verify(LINK_PURPOSE, media_id, expires, signature):
        raise InvalidLinkError
    media = await repository.get_ready_media(db, media_id)
    if media is None:
        raise MediaNotFoundError
    return storage.path(media.storage_key), media.content_type


async def delete_media(
    db: AsyncSession, owner_id: uuid.UUID, media_id: uuid.UUID
) -> None:
    media = await get_owned_media(db, owner_id, media_id)
    await repository.delete_media(db, media.id)
    await audit_service.record(
        db,
        AuditAction.MEDIA_DELETED,
        user_id=owner_id,
        target_type="media",
        target_id=media_id,
        details={"kind": media.kind, "camera_id": str(media.camera_id)},
    )
    await storage.delete(media.storage_key)


async def delete_all_for_owner(db: AsyncSession, owner_id: uuid.UUID) -> int:
    """Delete every file of a user whose account is being deleted. Rows go
    first, as in retention. Returns how many."""
    keys = await repository.take_owner_media(db, owner_id)
    for key in keys:
        await storage.delete(key)
    return len(keys)


# ---------- retention ----------


async def purge_expired(db: AsyncSession, now: datetime | None = None) -> int:
    """Delete media older than the retention period and abandoned uploads.
    Returns how many were removed. Rows go first, so a crash in between leaves
    at worst an orphaned file, never a row pointing at a missing file."""
    now = now or _utcnow()
    captured_before = now - timedelta(days=settings.media_retention_days)
    pending_before = now - PENDING_TTL
    removed = 0
    while keys := await repository.take_expired(
        db, captured_before, pending_before, PURGE_BATCH
    ):
        for key in keys:
            await storage.delete(key)
        removed += len(keys)
    return removed


async def run_retention_loop() -> None:
    """Purge forever, once per PURGE_INTERVAL. Started by the app on startup."""
    while True:
        try:
            async with SessionLocal() as db:
                removed = await purge_expired(db)
            if removed:
                logger.info("Media retention removed %d files", removed)
        except Exception:
            logger.exception("Media retention run failed")
        await asyncio.sleep(PURGE_INTERVAL.total_seconds())
