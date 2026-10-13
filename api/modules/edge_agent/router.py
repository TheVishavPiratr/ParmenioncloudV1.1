"""HTTP and WebSocket endpoints for the edge_agent module.

Two audiences:
- user_router  (/agents): people managing their agents, authenticated by user JWT.
- agent_router (/agent):  the on-site Edge Agent itself, authenticated by agent token.
"""

import uuid
from typing import Annotated

from fastapi import (
    APIRouter,
    Depends,
    HTTPException,
    Request,
    Response,
    WebSocket,
    WebSocketDisconnect,
    status,
)
from pydantic import ValidationError
from sqlalchemy.ext.asyncio import AsyncSession

from api.core.database import get_db
from api.modules.auth.dependencies import CurrentUser
from api.modules.edge_agent import service
from api.modules.edge_agent.connections import (
    CLOSE_UNAUTHORIZED,
    AgentConnection,
    manager,
)
from api.modules.edge_agent.dependencies import CurrentAgent
from api.modules.edge_agent.models import EdgeAgent
from api.modules.edge_agent.schemas import (
    AckMessage,
    AgentCreatedResponse,
    AgentCreateRequest,
    AgentHealth,
    AgentResponse,
    AgentSelfResponse,
    AgentUpdateRequest,
    CommandQueuedResponse,
    EnrollmentTokenResponse,
    EnrollRequest,
    EnrollResponse,
    HeartbeatRequest,
    HeartbeatResponse,
    MediaMtxStatus,
)

user_router = APIRouter(prefix="/agents", tags=["agents"])
agent_router = APIRouter(prefix="/agent", tags=["agent"])

DbSession = Annotated[AsyncSession, Depends(get_db)]

_HEARTBEAT_SECONDS = int(service.HEARTBEAT_INTERVAL.total_seconds())

_AGENT_NOT_FOUND = HTTPException(
    status_code=status.HTTP_404_NOT_FOUND, detail="Agent not found"
)


def _to_response(agent: EdgeAgent) -> AgentResponse:
    return AgentResponse(
        id=agent.id,
        name=agent.name,
        status=agent.status,
        online=service.is_online(agent),
        last_seen_at=agent.last_seen_at,
        last_ip=agent.last_ip,
        hostname=agent.hostname,
        platform=agent.platform,
        software_version=agent.software_version,
        mediamtx=MediaMtxStatus(
            running=agent.mediamtx_running, version=agent.mediamtx_version
        ),
        health=AgentHealth.model_validate(agent.last_health)
        if agent.last_health
        else None,
        config_version=agent.config_version,
        enrolled_at=agent.enrolled_at,
        created_at=agent.created_at,
    )


def _client_ip(request: Request) -> str | None:
    return request.client.host if request.client else None


# ---------- user-facing ----------


@user_router.post("", status_code=status.HTTP_201_CREATED)
async def create_agent(
    data: AgentCreateRequest, user: CurrentUser, db: DbSession
) -> AgentCreatedResponse:
    """Register a new site. The enrollment token is shown only in this response."""
    agent, token = await service.create_agent(db, user.id, data.name)
    return AgentCreatedResponse(
        agent=_to_response(agent),
        enrollment_token=token,
        enrollment_expires_at=agent.enrollment_expires_at,
    )


@user_router.get("")
async def list_agents(user: CurrentUser, db: DbSession) -> list[AgentResponse]:
    return [_to_response(agent) for agent in await service.list_agents(db, user.id)]


@user_router.get("/{agent_id}")
async def get_agent(
    agent_id: uuid.UUID, user: CurrentUser, db: DbSession
) -> AgentResponse:
    try:
        agent = await service.get_owned_agent(db, user.id, agent_id)
    except service.AgentNotFoundError:
        raise _AGENT_NOT_FOUND from None
    return _to_response(agent)


@user_router.patch("/{agent_id}")
async def rename_agent(
    agent_id: uuid.UUID, data: AgentUpdateRequest, user: CurrentUser, db: DbSession
) -> AgentResponse:
    try:
        agent = await service.rename_agent(db, user.id, agent_id, data.name)
    except service.AgentNotFoundError:
        raise _AGENT_NOT_FOUND from None
    return _to_response(agent)


@user_router.post("/{agent_id}/enrollment-token", status_code=status.HTTP_201_CREATED)
async def new_enrollment_token(
    agent_id: uuid.UUID, user: CurrentUser, db: DbSession
) -> EnrollmentTokenResponse:
    """New one-time token, e.g. for a re-installed or replacement box."""
    try:
        agent, token = await service.new_enrollment_token(db, user.id, agent_id)
    except service.AgentNotFoundError:
        raise _AGENT_NOT_FOUND from None
    return EnrollmentTokenResponse(
        enrollment_token=token, enrollment_expires_at=agent.enrollment_expires_at
    )


@user_router.post("/{agent_id}/revoke")
async def revoke_agent(
    agent_id: uuid.UUID, user: CurrentUser, db: DbSession
) -> AgentResponse:
    try:
        agent = await service.revoke_agent(db, user.id, agent_id)
    except service.AgentNotFoundError:
        raise _AGENT_NOT_FOUND from None
    return _to_response(agent)


@user_router.post("/{agent_id}/rescan", status_code=status.HTTP_202_ACCEPTED)
async def rescan_cameras(
    agent_id: uuid.UUID, user: CurrentUser, db: DbSession
) -> CommandQueuedResponse:
    """Ask the agent to scan its network for cameras now."""
    try:
        command = await service.request_rescan(db, user.id, agent_id)
    except service.AgentNotFoundError:
        raise _AGENT_NOT_FOUND from None
    except service.AgentNotActiveError:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT, detail="Agent is not active"
        ) from None
    return CommandQueuedResponse(command_id=command.id)


@user_router.delete("/{agent_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_agent(
    agent_id: uuid.UUID, user: CurrentUser, db: DbSession
) -> Response:
    try:
        await service.delete_agent(db, user.id, agent_id)
    except service.AgentNotFoundError:
        raise _AGENT_NOT_FOUND from None
    return Response(status_code=status.HTTP_204_NO_CONTENT)


# ---------- agent-facing ----------


@agent_router.post("/enroll")
async def enroll(
    data: EnrollRequest, request: Request, db: DbSession
) -> EnrollResponse:
    """Exchange a one-time enrollment token for the agent's long-lived token."""
    try:
        agent, token = await service.enroll(
            db,
            data.enrollment_token,
            hostname=data.hostname,
            platform=data.platform,
            software_version=data.software_version,
            ip=_client_ip(request),
        )
    except service.InvalidEnrollmentTokenError:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Invalid or expired enrollment token",
        ) from None
    return EnrollResponse(
        agent_id=agent.id,
        agent_token=token,
        heartbeat_interval_seconds=_HEARTBEAT_SECONDS,
    )


@agent_router.post("/heartbeat")
async def heartbeat(
    data: HeartbeatRequest, request: Request, agent: CurrentAgent, db: DbSession
) -> HeartbeatResponse:
    """Called every heartbeat interval. If config_version in the reply is newer
    than the agent's, it must re-fetch its config (safety net for missed commands)."""
    server_time = await service.record_heartbeat(db, agent, data, _client_ip(request))
    return HeartbeatResponse(
        server_time=server_time,
        config_version=agent.config_version,
        heartbeat_interval_seconds=_HEARTBEAT_SECONDS,
    )


@agent_router.get("/me")
async def agent_me(agent: CurrentAgent) -> AgentSelfResponse:
    return AgentSelfResponse(
        agent_id=agent.id,
        name=agent.name,
        status=agent.status,
        config_version=agent.config_version,
        heartbeat_interval_seconds=_HEARTBEAT_SECONDS,
    )


@agent_router.websocket("/ws")
async def agent_socket(websocket: WebSocket, db: DbSession) -> None:
    """Command channel. Protocol:

    cloud -> agent  {"type": "hello", "agent_id", "config_version"}   once, first
                    {"type": "command", "id", "command", "payload", "expires_at"}
                    {"type": "ack_received", "id"}                     reply to each ack
                    {"type": "error", "detail"}                        bad message
    agent -> cloud  {"type": "ack", "id", "ok": bool, "error"?}

    Close codes: 4401 bad/revoked token (don't retry with it),
                 4409 replaced by a newer connection of the same agent.
    Every open command is re-sent on reconnect, so a command id can repeat.
    """
    await websocket.accept()
    scheme, _, token = websocket.headers.get("authorization", "").strip().partition(" ")
    try:
        if scheme.lower() != "bearer":
            raise service.InvalidAgentTokenError
        agent = await service.get_agent_from_token(db, token.strip())
    except service.InvalidAgentTokenError:
        await websocket.close(code=CLOSE_UNAUTHORIZED)
        return

    connection = AgentConnection(websocket)
    try:
        # Hold the send lock through the handshake: a command pushed from
        # another request meanwhile waits, so hello always goes out first.
        # Registering before reading the queue means nothing created in
        # between is missed (at worst a command arrives twice).
        async with connection.lock:
            await manager.register(agent.id, connection)
            commands = await service.take_open_commands(db, agent.id)
            await websocket.send_json(
                {
                    "type": "hello",
                    "agent_id": str(agent.id),
                    "config_version": agent.config_version,
                }
            )
            for command in commands:
                await websocket.send_json(service.command_message(command))

        while True:
            message = await websocket.receive()
            if message["type"] == "websocket.disconnect":
                break
            raw = message.get("text") or message.get("bytes") or ""
            try:
                ack = AckMessage.model_validate_json(raw)
            except ValidationError:
                await connection.send(
                    {"type": "error", "detail": "Expected an ack message"}
                )
                continue
            await service.acknowledge_command(db, agent.id, ack)
            await connection.send({"type": "ack_received", "id": str(ack.id)})
    except (WebSocketDisconnect, RuntimeError):
        # Dropped by the agent, or closed by us (revoke, replaced) mid-send.
        pass
    finally:
        manager.unregister(agent.id, connection)
