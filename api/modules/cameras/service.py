"""Business logic for the cameras module.

Other modules may call: get_owned_camera, get_agent_camera, list_active_cameras,
is_online.

Whenever a change affects what an agent should stream, the change is committed
first and then edge_agent's notify_config_changed tells the agent to re-fetch
GET /agent/config.
"""

import secrets
import uuid
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any

from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from api.modules.audit import service as audit_service
from api.modules.cameras import credentials, repository
from api.modules.cameras.models import Camera, CameraState, StreamState
from api.modules.cameras.schemas import (
    AgentCameraConfig,
    AgentConfigResponse,
    CameraAdoptRequest,
    CameraCreateRequest,
    CameraStatus,
    CameraUpdateRequest,
    DiscoveredDevice,
    DiscoveryResult,
)
from api.modules.edge_agent import service as edge_agent_service

if TYPE_CHECKING:
    from api.modules.edge_agent.models import EdgeAgent

AuditAction = audit_service.AuditAction

# Active plus disabled cameras per agent.
MAX_CAMERAS_PER_AGENT = 64
# Every row per agent, including discovered and ignored ones, so a misbehaving
# agent cannot grow the table without limit.
MAX_ROWS_PER_AGENT = 512
# A camera counts as online while its last stream report is this recent.
STATUS_WINDOW = timedelta(seconds=90)
STREAM_PATH_PREFIX = "cam_"

# Fields the agent receives in its config; changing them means a re-fetch.
_AGENT_FIELDS = {
    "rtsp_url",
    "username",
    "password",
    "enabled",
    "record_enabled",
    "detection_enabled",
}
# Fields that decide whether the stream can connect; changing them makes the
# last stream report meaningless.
_STREAM_FIELDS = {"rtsp_url", "username", "password", "enabled"}


class CameraError(Exception):
    """Base of every error this service raises."""


class CameraNotFoundError(CameraError):
    pass


class AgentNotFoundError(CameraError):
    pass


class AgentNotActiveError(CameraError):
    """The agent has not enrolled yet, or was revoked."""


class CameraLimitError(CameraError):
    pass


class InvalidStateError(CameraError):
    pass


class MissingStreamUrlError(CameraError):
    pass


class DiscoveryConflictError(CameraError):
    """Another discovery report for the same agent was stored at the same time."""


def _utcnow() -> datetime:
    return datetime.now(UTC)


def _as_utc(value: datetime) -> datetime:
    # Some databases return naive datetimes; every stored time is UTC.
    return value if value.tzinfo else value.replace(tzinfo=UTC)


def _new_stream_path() -> str:
    return STREAM_PATH_PREFIX + secrets.token_hex(6)


def _reset_stream_status(camera: Camera) -> None:
    camera.stream_state = StreamState.UNKNOWN
    camera.stream_error = None
    camera.stream_checked_at = None


def _set_password(camera: Camera, password: str | None) -> None:
    camera.password_encrypted = credentials.encrypt(password) if password else None


async def _audit_camera(
    db: AsyncSession,
    action: AuditAction,
    camera: Camera,
    details: dict[str, Any] | None = None,
) -> None:
    await audit_service.record(
        db,
        action,
        user_id=camera.owner_id,
        target_type="camera",
        target_id=camera.id,
        details=details,
    )


def is_online(
    camera: Camera, agent: "EdgeAgent | None", now: datetime | None = None
) -> bool:
    """Active, its agent online, and a recent report says it is streaming."""
    now = now or _utcnow()
    return (
        camera.state == CameraState.ACTIVE
        and agent is not None
        and edge_agent_service.is_online(agent, now)
        and camera.stream_state == StreamState.STREAMING
        and camera.stream_checked_at is not None
        and now - _as_utc(camera.stream_checked_at) < STATUS_WINDOW
    )


async def _enrolled_agent(
    db: AsyncSession, owner_id: uuid.UUID, agent_id: uuid.UUID
) -> "EdgeAgent":
    try:
        agent = await edge_agent_service.get_owned_agent(db, owner_id, agent_id)
    except edge_agent_service.AgentNotFoundError:
        raise AgentNotFoundError from None
    # No setting up cameras before the box has enrolled, or after a revoke.
    if not edge_agent_service.is_enrolled(agent):
        raise AgentNotActiveError
    return agent


async def _check_capacity(db: AsyncSession, agent_id: uuid.UUID) -> None:
    if await repository.count_configured(db, agent_id) >= MAX_CAMERAS_PER_AGENT:
        raise CameraLimitError


# ---------- user actions ----------


async def get_owned_camera(
    db: AsyncSession, owner_id: uuid.UUID, camera_id: uuid.UUID
) -> Camera:
    camera = await repository.get_camera_for_owner(db, owner_id, camera_id)
    if camera is None:
        raise CameraNotFoundError
    return camera


async def list_cameras(
    db: AsyncSession,
    owner_id: uuid.UUID,
    agent_id: uuid.UUID | None = None,
    state: CameraState | None = None,
) -> list[Camera]:
    return await repository.list_cameras_for_owner(db, owner_id, agent_id, state)


async def with_online(
    db: AsyncSession, owner_id: uuid.UUID, cameras: list[Camera]
) -> list[tuple[Camera, bool]]:
    """Pair each camera with its online flag, loading the owner's agents once."""
    agents = {a.id: a for a in await edge_agent_service.list_agents(db, owner_id)}
    now = _utcnow()
    return [(c, is_online(c, agents.get(c.agent_id), now)) for c in cameras]


async def add_camera(
    db: AsyncSession, owner_id: uuid.UUID, data: CameraCreateRequest
) -> Camera:
    agent = await _enrolled_agent(db, owner_id, data.agent_id)
    await _check_capacity(db, agent.id)
    camera = Camera(
        owner_id=owner_id,
        agent_id=agent.id,
        state=CameraState.ACTIVE,
        name=data.name,
        rtsp_url=data.rtsp_url,
        username=data.username,
        stream_path=_new_stream_path(),
        record_enabled=data.record_enabled,
        detection_enabled=data.detection_enabled,
        stream_state=StreamState.UNKNOWN,
    )
    _set_password(camera, data.password)
    await repository.add_camera(db, camera)
    await _audit_camera(db, AuditAction.CAMERA_ADDED, camera, {"name": camera.name})
    await edge_agent_service.notify_config_changed(db, agent.id)
    return camera


async def adopt_camera(
    db: AsyncSession,
    owner_id: uuid.UUID,
    camera_id: uuid.UUID,
    data: CameraAdoptRequest,
) -> Camera:
    """Turn a discovered (or earlier ignored) camera into an active one."""
    camera = await get_owned_camera(db, owner_id, camera_id)
    if camera.state not in (CameraState.DISCOVERED, CameraState.IGNORED):
        raise InvalidStateError("Camera is already set up")
    await _enrolled_agent(db, owner_id, camera.agent_id)
    await _check_capacity(db, camera.agent_id)
    rtsp_url = data.rtsp_url or next(iter(camera.discovered_streams or []), None)
    if rtsp_url is None:
        raise MissingStreamUrlError

    camera.state = CameraState.ACTIVE
    camera.name = data.name
    camera.rtsp_url = rtsp_url
    camera.username = data.username
    _set_password(camera, data.password)
    camera.record_enabled = data.record_enabled
    camera.detection_enabled = data.detection_enabled
    _reset_stream_status(camera)
    await repository.commit(db)
    await _audit_camera(db, AuditAction.CAMERA_ADOPTED, camera, {"name": camera.name})
    await edge_agent_service.notify_config_changed(db, camera.agent_id)
    return camera


async def ignore_camera(
    db: AsyncSession, owner_id: uuid.UUID, camera_id: uuid.UUID
) -> Camera:
    camera = await get_owned_camera(db, owner_id, camera_id)
    if camera.state != CameraState.DISCOVERED:
        raise InvalidStateError("Only discovered cameras can be ignored")
    camera.state = CameraState.IGNORED
    await repository.commit(db)
    await _audit_camera(db, AuditAction.CAMERA_IGNORED, camera)
    return camera


async def update_camera(
    db: AsyncSession,
    owner_id: uuid.UUID,
    camera_id: uuid.UUID,
    data: CameraUpdateRequest,
) -> Camera:
    camera = await get_owned_camera(db, owner_id, camera_id)
    if camera.state not in (CameraState.ACTIVE, CameraState.DISABLED):
        raise InvalidStateError("Adopt the camera before changing it")
    changes = data.model_dump(exclude_unset=True)
    was_active = camera.state == CameraState.ACTIVE

    if "enabled" in changes:
        camera.state = (
            CameraState.ACTIVE if changes["enabled"] else CameraState.DISABLED
        )
    if "password" in changes:
        _set_password(camera, changes["password"])
    for field in (
        "name",
        "rtsp_url",
        "username",
        "record_enabled",
        "detection_enabled",
    ):
        if field in changes:
            setattr(camera, field, changes[field])
    if _STREAM_FIELDS & changes.keys():
        _reset_stream_status(camera)
    await repository.commit(db)
    # Field names only: the values may hold a password or credentials in a URL.
    await _audit_camera(
        db, AuditAction.CAMERA_UPDATED, camera, {"fields": sorted(changes)}
    )

    is_active = camera.state == CameraState.ACTIVE
    if (was_active or is_active) and _AGENT_FIELDS & changes.keys():
        await edge_agent_service.notify_config_changed(db, camera.agent_id)
    return camera


async def delete_camera(
    db: AsyncSession, owner_id: uuid.UUID, camera_id: uuid.UUID
) -> None:
    """Remove a camera. A discovered one comes back on the next rescan; use
    ignore_camera to keep it away."""
    camera = await get_owned_camera(db, owner_id, camera_id)
    was_active = camera.state == CameraState.ACTIVE
    await repository.delete_camera(db, camera.id)
    await _audit_camera(db, AuditAction.CAMERA_DELETED, camera)
    if was_active:
        await edge_agent_service.notify_config_changed(db, camera.agent_id)


# ---------- agent actions ----------


async def list_active_cameras(db: AsyncSession, agent_id: uuid.UUID) -> list[Camera]:
    return await repository.list_active_cameras(db, agent_id)


async def get_agent_camera(
    db: AsyncSession, agent_id: uuid.UUID, camera_id: uuid.UUID
) -> Camera:
    """An active or disabled camera of this agent. Disabled ones are included so
    footage captured just before the owner switched a camera off is kept."""
    camera = await repository.get_camera_for_agent(db, agent_id, camera_id)
    if camera is None or camera.state not in (
        CameraState.ACTIVE,
        CameraState.DISABLED,
    ):
        raise CameraNotFoundError
    return camera


async def build_agent_config(
    db: AsyncSession, agent: "EdgeAgent"
) -> AgentConfigResponse:
    """Everything the agent should stream, with decrypted passwords.

    config_version is the one the agent was loaded with. If a change lands
    between reading it and reading the cameras, that change also sends a new
    config_changed command, so the agent fetches again.
    """
    cameras = await repository.list_active_cameras(db, agent.id)
    return AgentConfigResponse(
        config_version=agent.config_version,
        cameras=[
            AgentCameraConfig(
                id=camera.id,
                stream_path=camera.stream_path,
                rtsp_url=camera.rtsp_url,
                username=camera.username,
                password=credentials.decrypt(camera.password_encrypted)
                if camera.password_encrypted
                else None,
                record=camera.record_enabled,
                detection=camera.detection_enabled,
            )
            for camera in cameras
        ],
    )


async def record_discovery(
    db: AsyncSession, agent: "EdgeAgent", devices: list[DiscoveredDevice]
) -> DiscoveryResult:
    """Store a network scan. New devices become discovered cameras; known ones
    get fresh network details but keep their state and settings."""
    now = _utcnow()
    latest = {device.device_key: device for device in devices}  # last one wins
    known = {
        camera.device_key: camera
        for camera in await repository.list_by_device_keys(db, agent.id, list(latest))
    }
    room = MAX_ROWS_PER_AGENT - await repository.count_cameras(db, agent.id)
    new = updated = skipped = 0

    for key, device in latest.items():
        camera = known.get(key)
        if camera is None:
            if room <= 0:
                skipped += 1
                continue
            room -= 1
            new += 1
            camera = Camera(
                owner_id=agent.owner_id,
                agent_id=agent.id,
                state=CameraState.DISCOVERED,
                device_key=key,
                stream_path=_new_stream_path(),
                record_enabled=False,
                detection_enabled=False,
                stream_state=StreamState.UNKNOWN,
            )
            repository.stage_camera(db, camera)
        else:
            updated += 1
        camera.ip_address = str(device.ip_address) if device.ip_address else None
        camera.vendor = device.vendor
        camera.model = device.model
        camera.discovered_streams = device.streams
        camera.last_discovered_at = now

    try:
        await repository.commit(db)
    except IntegrityError:
        raise DiscoveryConflictError from None
    return DiscoveryResult(new=new, updated=updated, skipped=skipped)


async def record_status(
    db: AsyncSession, agent: "EdgeAgent", reports: list[CameraStatus]
) -> int:
    """Store stream reports for the agent's own active cameras. Returns how
    many were stored."""
    now = _utcnow()
    updated = 0
    for report in reports:
        error = report.error if report.state == StreamState.ERROR else None
        if await repository.set_stream_status(
            db, agent.id, report.camera_id, StreamState(report.state), error, now
        ):
            updated += 1
    await repository.commit(db)
    return updated
