"""HTTP endpoints for the media_access module.

Three audiences:
- agent_router (/agent/media): the Edge Agent uploading files, by agent token.
- user_router  (/media):       people browsing their footage, by user JWT.
- user_router  (/media/files): anyone holding a signed link, no login header,
                               so <img> and <video> tags can load it directly.
"""

import uuid
from datetime import datetime
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response, status
from fastapi.responses import FileResponse
from sqlalchemy.ext.asyncio import AsyncSession

from api.core.database import get_db
from api.modules.auth.dependencies import CurrentUser
from api.modules.edge_agent.dependencies import CurrentAgent
from api.modules.media_access import service
from api.modules.media_access.models import Media, MediaKind
from api.modules.media_access.schemas import (
    MediaCreateRequest,
    MediaLinkResponse,
    MediaResponse,
    MediaUploadResponse,
)

agent_router = APIRouter(prefix="/agent/media", tags=["agent"])
user_router = APIRouter(prefix="/media", tags=["media"])

DbSession = Annotated[AsyncSession, Depends(get_db)]

_ERRORS: dict[type[service.MediaError], tuple[int, str]] = {
    service.MediaNotFoundError: (status.HTTP_404_NOT_FOUND, "Media not found"),
    service.CameraNotFoundError: (status.HTTP_404_NOT_FOUND, "Camera not found"),
    service.UploadLimitError: (
        status.HTTP_429_TOO_MANY_REQUESTS,
        f"Too many unfinished uploads (max {service.MAX_PENDING_PER_AGENT})",
    ),
    service.UploadTooLargeError: (
        status.HTTP_413_CONTENT_TOO_LARGE,
        "Upload is larger than the registered size_bytes",
    ),
    service.UploadMismatchError: (
        status.HTTP_400_BAD_REQUEST,
        "Upload does not match the registered size_bytes and sha256",
    ),
    # One answer for bad, expired and forged links.
    service.InvalidLinkError: (status.HTTP_403_FORBIDDEN, "Link is invalid or expired"),
}


def _http_error(exc: service.MediaError) -> HTTPException:
    code, detail = _ERRORS[type(exc)]
    return HTTPException(status_code=code, detail=detail)


def _to_response(media: Media) -> MediaResponse:
    return MediaResponse(
        id=media.id,
        camera_id=media.camera_id,
        agent_id=media.agent_id,
        kind=media.kind,
        content_type=media.content_type,
        size_bytes=media.size_bytes,
        sha256=media.sha256,
        duration_s=media.duration_s,
        captured_at=media.captured_at,
        uploaded_at=media.uploaded_at,
    )


def _upload_response(media: Media) -> MediaUploadResponse:
    return MediaUploadResponse(
        media_id=media.id,
        status=media.status,
        upload_url=f"/agent/media/{media.id}/content",
    )


# ---------- agent-facing ----------


@agent_router.post("", status_code=status.HTTP_201_CREATED)
async def register_upload(
    data: MediaCreateRequest, agent: CurrentAgent, db: DbSession
) -> MediaUploadResponse:
    """Step 1 of 2: announce a snapshot or clip. If the reply's status is
    "ready", the file is already in the cloud and step 2 can be skipped."""
    try:
        media = await service.register_upload(db, agent, data)
    except service.MediaError as exc:
        raise _http_error(exc) from None
    return _upload_response(media)


@agent_router.put("/{media_id}/content")
async def upload_content(
    media_id: uuid.UUID, request: Request, agent: CurrentAgent, db: DbSession
) -> MediaUploadResponse:
    """Step 2 of 2: send the raw file bytes as the request body. After a 200
    reply the agent may delete its local copy."""
    try:
        media = await service.upload_content(db, agent, media_id, request.stream())
    except service.MediaError as exc:
        raise _http_error(exc) from None
    return _upload_response(media)


# ---------- user-facing ----------


@user_router.get("")
async def list_media(
    user: CurrentUser,
    db: DbSession,
    camera_id: uuid.UUID | None = None,
    kind: MediaKind | None = None,
    before: datetime | None = None,
    limit: Annotated[int, Query(ge=1, le=100)] = 50,
) -> list[MediaResponse]:
    """Newest first. For the next page, pass the last item's captured_at as before."""
    items = await service.list_media(
        db, user.id, camera_id=camera_id, kind=kind, before=before, limit=limit
    )
    return [_to_response(media) for media in items]


@user_router.get("/files/{media_id}")
async def open_file(
    media_id: uuid.UUID,
    db: DbSession,
    exp: Annotated[int, Query()],
    sig: Annotated[str, Query(max_length=64)],
) -> FileResponse:
    """Serve a file through a signed link. Supports Range requests (seeking)."""
    try:
        path, content_type = await service.open_link(db, media_id, exp, sig)
    except service.MediaError as exc:
        raise _http_error(exc) from None
    if not path.is_file():
        raise _http_error(service.MediaNotFoundError())
    return FileResponse(
        path,
        media_type=content_type,
        headers={
            "Content-Disposition": "inline",
            "Cache-Control": "private, no-store",
            "X-Content-Type-Options": "nosniff",
        },
    )


@user_router.get("/{media_id}")
async def get_media(
    media_id: uuid.UUID, user: CurrentUser, db: DbSession
) -> MediaResponse:
    try:
        media = await service.get_owned_media(db, user.id, media_id)
    except service.MediaError as exc:
        raise _http_error(exc) from None
    return _to_response(media)


@user_router.post("/{media_id}/link")
async def create_link(
    media_id: uuid.UUID, user: CurrentUser, db: DbSession
) -> MediaLinkResponse:
    """A viewing link valid for a few minutes. Ask again when it expires."""
    try:
        url, expires_at = await service.create_link(db, user.id, media_id)
    except service.MediaError as exc:
        raise _http_error(exc) from None
    return MediaLinkResponse(url=url, expires_at=expires_at)


@user_router.delete("/{media_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_media(
    media_id: uuid.UUID, user: CurrentUser, db: DbSession
) -> Response:
    try:
        await service.delete_media(db, user.id, media_id)
    except service.MediaError as exc:
        raise _http_error(exc) from None
    return Response(status_code=status.HTTP_204_NO_CONTENT)
