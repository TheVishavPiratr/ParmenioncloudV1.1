"""HTTP endpoints for the audit module: a user's own activity log, read-only."""

from datetime import datetime
from typing import Annotated

from fastapi import APIRouter, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession

from api.core.database import get_db
from api.modules.audit import service
from api.modules.audit.models import AuditAction
from api.modules.audit.schemas import AuditEntryResponse
from api.modules.auth.dependencies import CurrentUser

router = APIRouter(prefix="/audit", tags=["audit"])

DbSession = Annotated[AsyncSession, Depends(get_db)]


@router.get("")
async def list_entries(
    user: CurrentUser,
    db: DbSession,
    action: AuditAction | None = None,
    before: datetime | None = None,
    limit: Annotated[int, Query(ge=1, le=100)] = 50,
) -> list[AuditEntryResponse]:
    """Everything recorded about this account, newest first: logins (including
    failed ones), devices, agents, cameras, footage and gallery changes. For
    the next page, pass the last item's created_at as before."""
    entries = await service.list_entries(
        db, user.id, action=action, before=before, limit=limit
    )
    return [
        AuditEntryResponse(
            id=entry.id,
            action=entry.action,
            success=entry.success,
            actor=entry.actor,
            agent_id=entry.agent_id,
            target_type=entry.target_type,
            target_id=entry.target_id,
            details=entry.details,
            ip=entry.ip,
            user_agent=entry.user_agent,
            created_at=entry.created_at,
        )
        for entry in entries
    ]
