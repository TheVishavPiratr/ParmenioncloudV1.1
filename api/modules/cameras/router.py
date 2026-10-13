"""HTTP endpoints for the cameras module.

Two audiences:
- user_router  (/cameras): people managing their cameras, authenticated by user JWT.
- agent_router (/agent):   the on-site Edge Agent, authenticated by agent token.
"""

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Response, status
from sqlalchemy.ext.asyncio import AsyncSession

from api.core.database import get_db
from api.modules.auth.dependencies import CurrentUser
from api.modules.cameras import service
from api.modules.cameras.models import Camera, CameraState
from api.modules.cameras.schemas import (
    AgentConfigResponse,
    CameraAdoptRequest,
    CameraCreateRequest,
    CameraResponse,
    CameraUpdateRequest,
    DiscoveryReport,
    DiscoveryResult,
    StatusReport,
    StatusResult,
)
from api.modules.edge_agent.dependencies import CurrentAgent

user_router = APIRouter(prefix="/cameras", tags=["cameras"])
agent_router = APIRouter(prefix="/agent", tags=["agent"])

DbSession = Annotated[AsyncSession, Depends(get_db)]

_ERRORS: dict[type[service.CameraError], tuple[int, str]] = {
    service.CameraNotFoundError: (status.HTTP_404_NOT_FOUND, "Camera not found"),
    service.AgentNotFoundError: (status.HTTP_404_NOT_FOUND, "Agent not found"),
    service.AgentNotActiveError: (status.HTTP_409_CONFLICT, "Agent is not active"),
    service.CameraLimitError: (
        status.HTTP_409_CONFLICT,
        f"An agent can have at most {service.MAX_CAMERAS_PER_AGENT} cameras",
    ),
    service.MissingStreamUrlError: (
        status.HTTP_422_UNPROCESSABLE_CONTENT,
        "rtsp_url is required: no stream was discovered for this camera",
    ),
    service.DiscoveryConflictError: (
        status.HTTP_409_CONFLICT,
        "Another discovery report was stored at the same time; send it again",
    ),
}


def _http_error(exc: service.CameraError) -> HTTPException:
    if isinstance(exc, service.InvalidStateError):
        return HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc))
    code, detail = _ERRORS[type(exc)]
    return HTTPException(status_code=code, detail=detail)


def _to_response(camera: Camera, online: bool) -> CameraResponse:
    return CameraResponse(
        id=camera.id,
        agent_id=camera.agent_id,
        name=camera.name,
        state=camera.state,
        online=online,
        ip_address=camera.ip_address,
        vendor=camera.vendor,
        model=camera.model,
        discovered_streams=camera.discovered_streams or [],
        rtsp_url=camera.rtsp_url,
        username=camera.username,
        has_password=camera.password_encrypted is not None,
        stream_path=camera.stream_path,
        record_enabled=camera.record_enabled,
        detection_enabled=camera.detection_enabled,
        stream_state=camera.stream_state,
        stream_error=camera.stream_error,
        stream_checked_at=camera.stream_checked_at,
        last_discovered_at=camera.last_discovered_at,
        created_at=camera.created_at,
    )


async def _respond(
    db: AsyncSession, owner_id: uuid.UUID, camera: Camera
) -> CameraResponse:
    [(camera, online)] = await service.with_online(db, owner_id, [camera])
    return _to_response(camera, online)


# ---------- user-facing ----------


@user_router.get("")
async def list_cameras(
    user: CurrentUser,
    db: DbSession,
    agent_id: uuid.UUID | None = None,
    state: CameraState | None = None,
) -> list[CameraResponse]:
    cameras = await service.list_cameras(db, user.id, agent_id, state)
    return [
        _to_response(camera, online)
        for camera, online in await service.with_online(db, user.id, cameras)
    ]


@user_router.post("", status_code=status.HTTP_201_CREATED)
async def add_camera(
    data: CameraCreateRequest, user: CurrentUser, db: DbSession
) -> CameraResponse:
    """Add a camera by hand. The agent must be enrolled."""
    try:
        camera = await service.add_camera(db, user.id, data)
    except service.CameraError as exc:
        raise _http_error(exc) from None
    return await _respond(db, user.id, camera)


@user_router.get("/{camera_id}")
async def get_camera(
    camera_id: uuid.UUID, user: CurrentUser, db: DbSession
) -> CameraResponse:
    try:
        camera = await service.get_owned_camera(db, user.id, camera_id)
    except service.CameraError as exc:
        raise _http_error(exc) from None
    return await _respond(db, user.id, camera)


@user_router.post("/{camera_id}/adopt")
async def adopt_camera(
    camera_id: uuid.UUID, data: CameraAdoptRequest, user: CurrentUser, db: DbSession
) -> CameraResponse:
    """Set up a camera found by a scan, so the agent starts streaming it."""
    try:
        camera = await service.adopt_camera(db, user.id, camera_id, data)
    except service.CameraError as exc:
        raise _http_error(exc) from None
    return await _respond(db, user.id, camera)


@user_router.post("/{camera_id}/ignore")
async def ignore_camera(
    camera_id: uuid.UUID, user: CurrentUser, db: DbSession
) -> CameraResponse:
    """Dismiss a discovered camera so rescans don't offer it again."""
    try:
        camera = await service.ignore_camera(db, user.id, camera_id)
    except service.CameraError as exc:
        raise _http_error(exc) from None
    return await _respond(db, user.id, camera)


@user_router.patch("/{camera_id}")
async def update_camera(
    camera_id: uuid.UUID, data: CameraUpdateRequest, user: CurrentUser, db: DbSession
) -> CameraResponse:
    try:
        camera = await service.update_camera(db, user.id, camera_id, data)
    except service.CameraError as exc:
        raise _http_error(exc) from None
    return await _respond(db, user.id, camera)


@user_router.delete("/{camera_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_camera(
    camera_id: uuid.UUID, user: CurrentUser, db: DbSession
) -> Response:
    try:
        await service.delete_camera(db, user.id, camera_id)
    except service.CameraError as exc:
        raise _http_error(exc) from None
    return Response(status_code=status.HTTP_204_NO_CONTENT)


# ---------- agent-facing ----------


@agent_router.get("/config")
async def agent_config(agent: CurrentAgent, db: DbSession) -> AgentConfigResponse:
    """The cameras to stream. Fetched on connect, on a config_changed command,
    and when a heartbeat reply shows a newer config_version."""
    return await service.build_agent_config(db, agent)


@agent_router.post("/cameras/discovered")
async def report_discovered(
    data: DiscoveryReport, agent: CurrentAgent, db: DbSession
) -> DiscoveryResult:
    """Results of a network scan, usually after a rescan_cameras command."""
    try:
        return await service.record_discovery(db, agent, data.devices)
    except service.CameraError as exc:
        raise _http_error(exc) from None


@agent_router.post("/cameras/status")
async def report_status(
    data: StatusReport, agent: CurrentAgent, db: DbSession
) -> StatusResult:
    """Stream state of active cameras; send about every heartbeat and on change."""
    updated = await service.record_status(db, agent, data.cameras)
    return StatusResult(updated=updated)
