"""Business logic for the account module.

Deleting an account removes everything the user has in the cloud. This
module sits above the others: each one deletes what it owns, in an order
that keeps the account consistent if the request dies halfway (retrying it
finishes the job):

1. edge_agent: agents are deleted and their sockets cut, so no box keeps
   uploading. Their cameras and commands cascade.
2. media_access and gallery: rows, then files on disk.
3. audit: the user's entries, which hold their IP addresses and devices.
4. auth: the user row. Devices, codes, events, notifications and settings
   cascade from it.

One anonymous entry is then kept in the audit log, and a confirmation is
emailed to the address the account had.

The module has no tables of its own, so no models or repository.
"""

from typing import TYPE_CHECKING

from sqlalchemy.ext.asyncio import AsyncSession

from api.modules.audit import service as audit_service
from api.modules.auth import service as auth_service
from api.modules.edge_agent import service as edge_agent_service
from api.modules.gallery import service as gallery_service
from api.modules.media_access import service as media_service
from api.modules.notifications import service as notifications_service

if TYPE_CHECKING:
    from api.modules.auth.models import User

AuditAction = audit_service.AuditAction


class WrongPasswordError(Exception):
    pass


async def delete_account(db: AsyncSession, user: "User", password: str) -> None:
    """Delete the user and everything they own, for good."""
    if not await auth_service.confirm_password(user, password):
        await audit_service.record(
            db,
            AuditAction.ACCOUNT_DELETION_FAILED,
            user_id=user.id,
            success=False,
            details={"reason": "wrong_password"},
        )
        raise WrongPasswordError

    user_id, email = user.id, user.email
    agents = await edge_agent_service.delete_all_for_owner(db, user_id)
    media = await media_service.delete_all_for_owner(db, user_id)
    faces = await gallery_service.delete_all_for_owner(db, user_id)
    await audit_service.forget_user(db, user_id)
    await auth_service.delete_user(db, user_id)

    # No user_id (it is gone) and no email: only that an account was deleted.
    await audit_service.record(
        db,
        AuditAction.ACCOUNT_DELETED,
        user_id=None,
        target_type="user",
        target_id=user_id,
        details={"agents": agents, "media_files": media, "face_photos": faces},
    )
    await notifications_service.send_email(
        email,
        "Your VisionPulse account was deleted",
        "Your VisionPulse account and everything in it (edge boxes, cameras, "
        "footage, gallery photos, events and notifications) has been deleted "
        "for good.\n\nIf you didn't do this, someone knew your password: "
        "contact support right away.",
    )
