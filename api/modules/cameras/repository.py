"""Database access for the cameras module."""

import uuid
from datetime import datetime

from sqlalchemy import delete, func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from api.modules.cameras.models import Camera, CameraState, StreamState

_CONFIGURED = (CameraState.ACTIVE, CameraState.DISABLED)


async def commit(db: AsyncSession) -> None:
    try:
        await db.commit()
    except Exception:
        await db.rollback()
        raise


async def add_camera(db: AsyncSession, camera: Camera) -> Camera:
    db.add(camera)
    await commit(db)
    return camera


def stage_camera(db: AsyncSession, camera: Camera) -> None:
    """Add a camera to the session without committing."""
    db.add(camera)


async def get_camera_for_owner(
    db: AsyncSession, owner_id: uuid.UUID, camera_id: uuid.UUID
) -> Camera | None:
    return await db.scalar(
        select(Camera).where(Camera.id == camera_id, Camera.owner_id == owner_id)
    )


async def get_camera_for_agent(
    db: AsyncSession, agent_id: uuid.UUID, camera_id: uuid.UUID
) -> Camera | None:
    return await db.scalar(
        select(Camera).where(Camera.id == camera_id, Camera.agent_id == agent_id)
    )


async def list_cameras_for_owner(
    db: AsyncSession,
    owner_id: uuid.UUID,
    agent_id: uuid.UUID | None = None,
    state: str | None = None,
) -> list[Camera]:
    query = select(Camera).where(Camera.owner_id == owner_id)
    if agent_id is not None:
        query = query.where(Camera.agent_id == agent_id)
    if state is not None:
        query = query.where(Camera.state == state)
    result = await db.scalars(query.order_by(Camera.created_at, Camera.id))
    return list(result)


async def list_active_cameras(db: AsyncSession, agent_id: uuid.UUID) -> list[Camera]:
    result = await db.scalars(
        select(Camera)
        .where(Camera.agent_id == agent_id, Camera.state == CameraState.ACTIVE)
        .order_by(Camera.created_at, Camera.id)
    )
    return list(result)


async def list_by_device_keys(
    db: AsyncSession, agent_id: uuid.UUID, device_keys: list[str]
) -> list[Camera]:
    if not device_keys:
        return []
    result = await db.scalars(
        select(Camera).where(
            Camera.agent_id == agent_id, Camera.device_key.in_(device_keys)
        )
    )
    return list(result)


async def count_configured(db: AsyncSession, agent_id: uuid.UUID) -> int:
    """Active plus disabled cameras of the agent."""
    return await db.scalar(
        select(func.count())
        .select_from(Camera)
        .where(Camera.agent_id == agent_id, Camera.state.in_(_CONFIGURED))
    )


async def count_cameras(db: AsyncSession, agent_id: uuid.UUID) -> int:
    """Every camera row of the agent, in any state."""
    return await db.scalar(
        select(func.count()).select_from(Camera).where(Camera.agent_id == agent_id)
    )


async def delete_camera(db: AsyncSession, camera_id: uuid.UUID) -> None:
    await db.execute(
        delete(Camera)
        .where(Camera.id == camera_id)
        .execution_options(synchronize_session="fetch")
    )
    await commit(db)


async def set_stream_status(
    db: AsyncSession,
    agent_id: uuid.UUID,
    camera_id: uuid.UUID,
    state: StreamState,
    error: str | None,
    now: datetime,
) -> bool:
    """Store one stream report, no commit.

    Returns False if the camera is unknown, another agent's, or not active.
    """
    result = await db.execute(
        update(Camera)
        .where(
            Camera.id == camera_id,
            Camera.agent_id == agent_id,
            Camera.state == CameraState.ACTIVE,
        )
        .values(stream_state=state, stream_error=error, stream_checked_at=now)
        .execution_options(synchronize_session="fetch")
    )
    return result.rowcount == 1
