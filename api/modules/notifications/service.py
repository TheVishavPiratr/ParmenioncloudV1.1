"""Business logic for the notifications module.

A notification always lands in the user's in-app inbox. Depending on the
user's settings for its category, it also goes out as a push alert to every
linked device with alerts switched on, and as an email.

Other modules may call: notify, send_email, NotificationCategory.
- notify: tell a user something happened. Call it after the action has been
  committed; it commits the session itself and never raises.
- send_email: a message that must go out whatever the settings, such as a
  login code.

Delivery runs inside the request, with short timeouts. A failed delivery is
logged and never fails the action that caused it.
"""

import asyncio
import logging
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from api.core.config import settings
from api.core.database import SessionLocal
from api.modules.audit import service as audit_service
from api.modules.auth import service as auth_service
from api.modules.device_link import service as device_link_service
from api.modules.notifications import mailer, push, repository
from api.modules.notifications.models import (
    Notification,
    NotificationCategory,
    NotificationPreference,
)
from api.modules.notifications.push import PushResult

logger = logging.getLogger(__name__)

PURGE_INTERVAL = timedelta(hours=6)
PURGE_BATCH = 1000


@dataclass(frozen=True)
class Channels:
    push: bool
    email: bool


# What a user gets before changing anything. Security alerts also come by
# email: someone who took over the account may have signed the phone out.
DEFAULTS: dict[NotificationCategory, Channels] = {
    NotificationCategory.SECURITY: Channels(push=True, email=True),
    NotificationCategory.SYSTEM: Channels(push=True, email=False),
    NotificationCategory.DETECTION: Channels(push=True, email=False),
}


class NotificationNotFoundError(Exception):
    pass


def _utcnow() -> datetime:
    return datetime.now(UTC)


# ---------- transports ----------


async def send_email(to: str, subject: str, body: str) -> None:
    """Deliver an email using the transport selected by EMAIL_MODE. A failure
    is logged, not raised: the user can always ask for the email again."""
    if settings.email_mode == "console":
        # Development transport: show the email in the server terminal.
        print(
            f"\n===== EMAIL (console mode) =====\n"
            f"To: {to}\nSubject: {subject}\n\n{body}\n"
            f"================================\n",
            flush=True,
        )
        return
    try:
        await mailer.send(to, subject, body)
    except Exception:
        logger.exception("Could not send email %r", subject)


async def send_push(
    provider: str, token: str, title: str, body: str, data: dict[str, str]
) -> PushResult:
    """Deliver one push alert using the transport selected by PUSH_MODE."""
    if settings.push_mode == "console":
        print(
            f"\n===== PUSH (console mode) =====\n"
            f"To: {provider}:{token[:12]}...\n{title}\n{body}\n{data}\n"
            f"===============================\n",
            flush=True,
        )
        return PushResult.SENT
    if provider != "fcm":
        return PushResult.FAILED
    return await push.fcm().send(token, title, body, data)


async def _push_to_devices(
    db: AsyncSession, user_id: uuid.UUID, title: str, body: str, data: dict[str, str]
) -> tuple[int, int]:
    """Push to every device of the user with alerts on, all at once. Tokens
    the push service no longer knows are dropped. Returns (devices, delivered)."""
    targets = await device_link_service.list_push_targets(db, user_id)
    results = await asyncio.gather(
        *(send_push(t.provider, t.token, title, body, data) for t in targets)
    )
    for target, result in zip(targets, results, strict=True):
        if result is PushResult.INVALID_TOKEN:
            await device_link_service.drop_push_token(db, target.provider, target.token)
    return len(targets), sum(result is PushResult.SENT for result in results)


# ---------- for other modules ----------


async def notify(
    db: AsyncSession,
    user_id: uuid.UUID,
    category: NotificationCategory,
    kind: str,
    title: str,
    body: str,
    data: dict[str, str] | None = None,
) -> None:
    """Put a notification in the user's inbox and deliver it on the channels
    the user chose for its category. Never raises."""
    notification = Notification(
        id=uuid.uuid4(),
        user_id=user_id,
        category=category,
        kind=kind,
        title=title,
        body=body,
        data=data,
        created_at=_utcnow(),
    )
    try:
        await repository.add_notification(db, notification)
    except Exception:
        logger.exception("Could not store notification %s", kind)

    try:
        channels = await get_channels(db, user_id, category)
        if channels.push:
            payload = {
                **(data or {}),
                "notification_id": str(notification.id),
                "category": category,
                "kind": kind,
            }
            await _push_to_devices(db, user_id, title, body, payload)
        if channels.email:
            email = await auth_service.get_email(db, user_id)
            if email is not None:
                await send_email(
                    email,
                    f"VisionPulse: {title}",
                    f"{body}\n\nYou get this email because of your VisionPulse "
                    f"notification settings for {category} alerts.",
                )
    except Exception:
        logger.exception("Could not deliver notification %s", kind)


# ---------- inbox ----------


async def list_notifications(
    db: AsyncSession,
    user_id: uuid.UUID,
    *,
    unread_only: bool = False,
    before: datetime | None = None,
    limit: int = 50,
) -> list[Notification]:
    return await repository.list_notifications(
        db, user_id, unread_only=unread_only, before=before, limit=limit
    )


async def count_unread(db: AsyncSession, user_id: uuid.UUID) -> int:
    return await repository.count_unread(db, user_id)


async def mark_read(
    db: AsyncSession, user_id: uuid.UUID, notification_id: uuid.UUID
) -> Notification:
    notification = await repository.get_notification(db, user_id, notification_id)
    if notification is None:
        raise NotificationNotFoundError
    if notification.read_at is None:
        notification.read_at = _utcnow()
        await repository.commit(db)
    return notification


async def mark_all_read(db: AsyncSession, user_id: uuid.UUID) -> int:
    return await repository.mark_all_read(db, user_id, _utcnow())


async def delete_notification(
    db: AsyncSession, user_id: uuid.UUID, notification_id: uuid.UUID
) -> None:
    notification = await repository.get_notification(db, user_id, notification_id)
    if notification is None:
        raise NotificationNotFoundError
    await repository.delete_notification(db, notification.id)


# ---------- settings ----------


async def get_channels(
    db: AsyncSession, user_id: uuid.UUID, category: NotificationCategory
) -> Channels:
    preference = await repository.get_preference(db, user_id, category)
    if preference is None:
        return DEFAULTS[category]
    return Channels(push=preference.push, email=preference.email)


async def list_channels(
    db: AsyncSession, user_id: uuid.UUID
) -> list[tuple[NotificationCategory, Channels]]:
    """Every category with the user's channels, defaults filled in."""
    stored = {p.category: p for p in await repository.list_preferences(db, user_id)}
    result = []
    for category in NotificationCategory:
        preference = stored.get(category)
        channels = (
            Channels(push=preference.push, email=preference.email)
            if preference
            else DEFAULTS[category]
        )
        result.append((category, channels))
    return result


async def update_channels(
    db: AsyncSession,
    user_id: uuid.UUID,
    category: NotificationCategory,
    *,
    push: bool | None = None,
    email: bool | None = None,
) -> Channels:
    """Change one category's channels; a channel left as None keeps its value."""
    for attempt in range(2):
        preference = await repository.get_preference(db, user_id, category)
        if preference is None:
            default = DEFAULTS[category]
            preference = NotificationPreference(
                user_id=user_id,
                category=category,
                push=default.push,
                email=default.email,
            )
            repository.stage(db, preference)
        if push is not None:
            preference.push = push
        if email is not None:
            preference.email = email
        preference.updated_at = _utcnow()
        try:
            await repository.commit(db)
            break
        except IntegrityError:
            # A parallel request created the row first; apply on top of it.
            if attempt:
                raise
    channels = Channels(push=preference.push, email=preference.email)
    await audit_service.record(
        db,
        audit_service.AuditAction.NOTIFICATION_SETTINGS_UPDATED,
        user_id=user_id,
        details={"category": category, "push": channels.push, "email": channels.email},
    )
    return channels


async def send_test_push(db: AsyncSession, user_id: uuid.UUID) -> tuple[int, int]:
    """A test alert to every device with alerts on, whatever the category
    settings, and not kept in the inbox. Returns (devices, delivered)."""
    return await _push_to_devices(
        db,
        user_id,
        "Test alert",
        "Alerts from VisionPulse reach this device.",
        {"kind": "test"},
    )


# ---------- retention ----------


async def purge_expired(db: AsyncSession, now: datetime | None = None) -> int:
    """Delete notifications older than the retention period. Returns how many."""
    cutoff = (now or _utcnow()) - timedelta(days=settings.notification_retention_days)
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
                logger.info("Notification retention removed %d entries", removed)
        except Exception:
            logger.exception("Notification retention run failed")
        await asyncio.sleep(PURGE_INTERVAL.total_seconds())
