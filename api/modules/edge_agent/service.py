"""Business logic for the edge_agent module.

Other modules may call: get_owned_agent, list_agents, is_enrolled, is_online,
send_command, notify_config_changed, delete_all_for_owner.
"""

import hashlib
import secrets
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from api.modules.audit import service as audit_service
from api.modules.auth import service as auth_service
from api.modules.edge_agent import repository
from api.modules.edge_agent.connections import CLOSE_UNAUTHORIZED, manager
from api.modules.edge_agent.models import (
    AgentCommand,
    AgentStatus,
    CommandStatus,
    CommandType,
    EdgeAgent,
)
from api.modules.edge_agent.schemas import AckMessage, AgentHealth, HeartbeatRequest
from api.modules.notifications import service as notifications_service

AuditAction = audit_service.AuditAction
AuditActor = audit_service.AuditActor

ENROLLMENT_TOKEN_PREFIX = "vpe_"
AGENT_TOKEN_PREFIX = "vpa_"
ENROLLMENT_TTL = timedelta(hours=24)
HEARTBEAT_INTERVAL = timedelta(seconds=30)
# Online while the last heartbeat is newer than three missed intervals.
ONLINE_WINDOW = 3 * HEARTBEAT_INTERVAL
COMMAND_TTL = {
    CommandType.CONFIG_CHANGED: timedelta(minutes=5),
    CommandType.RESCAN_CAMERAS: timedelta(minutes=2),
}


class AgentNotFoundError(Exception):
    pass


class AgentNotActiveError(Exception):
    pass


class InvalidEnrollmentTokenError(Exception):
    pass


class InvalidAgentTokenError(Exception):
    pass


def _utcnow() -> datetime:
    return datetime.now(UTC)


def _as_utc(value: datetime) -> datetime:
    # Some databases return naive datetimes; every stored time is UTC.
    return value if value.tzinfo else value.replace(tzinfo=UTC)


def _new_token(prefix: str) -> str:
    return prefix + secrets.token_urlsafe(32)


def _hash_token(token: str) -> str:
    # Tokens are 256-bit random values, so a fast unsalted hash is enough:
    # there is nothing to brute-force, and every agent request stays cheap.
    return hashlib.sha256(token.encode()).hexdigest()


async def _audit_agent(
    db: AsyncSession,
    action: AuditAction,
    agent: EdgeAgent,
    *,
    by_agent: bool = False,
    details: dict[str, Any] | None = None,
) -> None:
    """Record an action on an agent, done by its owner or (by_agent) by the box."""
    await audit_service.record(
        db,
        action,
        user_id=agent.owner_id,
        agent_id=agent.id if by_agent else None,
        target_type="agent",
        target_id=agent.id,
        details=details,
    )


async def _enrollment_failed(
    db: AsyncSession, agent: EdgeAgent | None, reason: str
) -> None:
    """Without a matching agent the attempt belongs to nobody, and only the
    database shows it."""
    await audit_service.record(
        db,
        AuditAction.AGENT_ENROLLMENT_FAILED,
        user_id=agent.owner_id if agent else None,
        success=False,
        actor=AuditActor.ANONYMOUS,
        target_type="agent" if agent else None,
        target_id=agent.id if agent else None,
        details={"reason": reason},
    )


def is_enrolled(agent: EdgeAgent) -> bool:
    """True once the box has enrolled, and until the owner revokes it."""
    return agent.status == AgentStatus.ACTIVE


def is_online(agent: EdgeAgent, now: datetime | None = None) -> bool:
    if agent.status != AgentStatus.ACTIVE or agent.last_seen_at is None:
        return False
    return (now or _utcnow()) - _as_utc(agent.last_seen_at) < ONLINE_WINDOW


# ---------- owner actions ----------


async def create_agent(
    db: AsyncSession, owner_id: uuid.UUID, name: str
) -> tuple[EdgeAgent, str]:
    """Create a pending agent. Returns it with its one-time enrollment token."""
    token = _new_token(ENROLLMENT_TOKEN_PREFIX)
    now = _utcnow()
    agent = EdgeAgent(
        owner_id=owner_id,
        name=name,
        status=AgentStatus.PENDING,
        enrollment_token_hash=_hash_token(token),
        enrollment_expires_at=now + ENROLLMENT_TTL,
        config_version=1,
        created_at=now,
        updated_at=now,
    )
    await repository.add_agent(db, agent)
    await _audit_agent(db, AuditAction.AGENT_CREATED, agent, details={"name": name})
    return agent, token


async def list_agents(db: AsyncSession, owner_id: uuid.UUID) -> list[EdgeAgent]:
    return await repository.list_agents_for_owner(db, owner_id)


async def get_owned_agent(
    db: AsyncSession, owner_id: uuid.UUID, agent_id: uuid.UUID
) -> EdgeAgent:
    """Return the agent if this user owns it. Raises AgentNotFoundError otherwise,
    so a user can't tell someone else's agent apart from a missing one."""
    agent = await repository.get_agent_for_owner(db, owner_id, agent_id)
    if agent is None:
        raise AgentNotFoundError
    return agent


async def rename_agent(
    db: AsyncSession, owner_id: uuid.UUID, agent_id: uuid.UUID, name: str
) -> EdgeAgent:
    agent = await get_owned_agent(db, owner_id, agent_id)
    agent.name = name
    await repository.commit(db)
    await _audit_agent(db, AuditAction.AGENT_RENAMED, agent, details={"name": name})
    return agent


async def new_enrollment_token(
    db: AsyncSession, owner_id: uuid.UUID, agent_id: uuid.UUID
) -> tuple[EdgeAgent, str]:
    """Issue a fresh enrollment token, e.g. to install on a replacement box.

    Any earlier enrollment token stops working. The current box keeps working
    until the new token is used. Returns the agent and the plain token.
    """
    agent = await get_owned_agent(db, owner_id, agent_id)
    token = _new_token(ENROLLMENT_TOKEN_PREFIX)
    agent.enrollment_token_hash = _hash_token(token)
    agent.enrollment_expires_at = _utcnow() + ENROLLMENT_TTL
    await repository.commit(db)
    await _audit_agent(db, AuditAction.AGENT_ENROLLMENT_TOKEN_ISSUED, agent)
    return agent, token


async def revoke_agent(
    db: AsyncSession, owner_id: uuid.UUID, agent_id: uuid.UUID
) -> EdgeAgent:
    """Cancel the agent's tokens at once and cut its WebSocket."""
    agent = await get_owned_agent(db, owner_id, agent_id)
    agent.status = AgentStatus.REVOKED
    agent.token_hash = None
    agent.enrollment_token_hash = None
    agent.enrollment_expires_at = None
    await repository.expire_open_commands(db, agent.id)
    await repository.commit(db)
    await _audit_agent(db, AuditAction.AGENT_REVOKED, agent)
    await manager.disconnect(agent.id, CLOSE_UNAUTHORIZED)
    return agent


async def delete_agent(
    db: AsyncSession, owner_id: uuid.UUID, agent_id: uuid.UUID
) -> None:
    agent = await get_owned_agent(db, owner_id, agent_id)
    await repository.delete_agent(db, agent.id)
    await audit_service.record(
        db,
        AuditAction.AGENT_DELETED,
        user_id=owner_id,
        target_type="agent",
        target_id=agent_id,
    )
    await manager.disconnect(agent_id, CLOSE_UNAUTHORIZED)


async def delete_all_for_owner(db: AsyncSession, owner_id: uuid.UUID) -> int:
    """Delete every agent of a user whose account is being deleted, and cut
    their sockets, so no box keeps uploading meanwhile. Returns how many."""
    agents = await repository.list_agents_for_owner(db, owner_id)
    for agent in agents:
        await repository.delete_agent(db, agent.id)
    for agent in agents:
        await manager.disconnect(agent.id, CLOSE_UNAUTHORIZED)
    return len(agents)


async def request_rescan(
    db: AsyncSession, owner_id: uuid.UUID, agent_id: uuid.UUID
) -> AgentCommand:
    agent = await get_owned_agent(db, owner_id, agent_id)
    if agent.status != AgentStatus.ACTIVE:
        raise AgentNotActiveError
    # Tapping Rescan repeatedly must not queue scans to run back to back.
    existing = await repository.get_open_command(
        db, agent.id, CommandType.RESCAN_CAMERAS, _utcnow()
    )
    if existing is not None:
        return existing
    return await send_command(db, agent.id, CommandType.RESCAN_CAMERAS)


# ---------- agent actions ----------


async def enroll(
    db: AsyncSession,
    enrollment_token: str,
    *,
    hostname: str,
    platform: str,
    software_version: str,
    ip: str | None,
) -> tuple[EdgeAgent, str]:
    """Swap a one-time enrollment token for a long-lived agent token.

    Re-enrolling an already active agent (a replacement box) gives it a new
    agent token; the old box's token stops working.
    """
    if not enrollment_token.startswith(ENROLLMENT_TOKEN_PREFIX):
        await _enrollment_failed(db, None, "malformed_token")
        raise InvalidEnrollmentTokenError
    enrollment_hash = _hash_token(enrollment_token)
    agent = await repository.get_agent_by_enrollment_hash(db, enrollment_hash)
    if agent is None:
        await _enrollment_failed(db, None, "unknown_token")
        raise InvalidEnrollmentTokenError
    if not await auth_service.is_active_user(db, agent.owner_id):
        await _enrollment_failed(db, agent, "owner_disabled")
        raise InvalidEnrollmentTokenError

    now = _utcnow()
    agent_token = _new_token(AGENT_TOKEN_PREFIX)
    claimed = await repository.claim_enrollment(
        db,
        agent.id,
        enrollment_hash,
        now,
        {
            "status": AgentStatus.ACTIVE,
            "token_hash": _hash_token(agent_token),
            "enrolled_at": now,
            "last_seen_at": now,
            "last_ip": ip,
            "hostname": hostname,
            "platform": platform,
            "software_version": software_version,
            "updated_at": now,
        },
    )
    if not claimed:
        await repository.commit(db)
        await _enrollment_failed(db, agent, "expired_or_used")
        raise InvalidEnrollmentTokenError
    await repository.commit(db)
    await _audit_agent(
        db,
        AuditAction.AGENT_ENROLLED,
        agent,
        by_agent=True,
        details={"hostname": hostname, "platform": platform},
    )
    await notifications_service.notify(
        db,
        agent.owner_id,
        notifications_service.NotificationCategory.SYSTEM,
        "agent_enrolled",
        "Edge box connected",
        f"{agent.name} is now connected from {hostname}. If you didn't set up "
        f"a box, revoke it under Agents.",
        {"agent_id": str(agent.id)},
    )
    # A socket still open from the previous box now holds a dead token.
    await manager.disconnect(agent.id, CLOSE_UNAUTHORIZED)
    return agent, agent_token


async def get_agent_from_token(db: AsyncSession, token: str) -> EdgeAgent:
    if not token.startswith(AGENT_TOKEN_PREFIX):
        raise InvalidAgentTokenError
    agent = await repository.get_active_agent_by_token_hash(db, _hash_token(token))
    if agent is None or not await auth_service.is_active_user(db, agent.owner_id):
        raise InvalidAgentTokenError
    return agent


async def record_heartbeat(
    db: AsyncSession, agent: EdgeAgent, data: HeartbeatRequest, ip: str | None
) -> datetime:
    """Store the agent's health report. Returns the server time."""
    now = _utcnow()
    agent.last_seen_at = now
    agent.last_ip = ip
    agent.software_version = data.software_version
    agent.mediamtx_running = data.mediamtx.running
    agent.mediamtx_version = data.mediamtx.version
    agent.last_health = AgentHealth(
        uptime_s=data.uptime_s,
        cpu_pct=data.cpu_pct,
        mem_pct=data.mem_pct,
        disk_pct=data.disk_pct,
        mediamtx_active_paths=data.mediamtx.active_paths,
    ).model_dump()
    await repository.commit(db)
    return now


# ---------- commands ----------


def command_message(command: AgentCommand) -> dict[str, Any]:
    """The JSON pushed to the agent over its WebSocket."""
    return {
        "type": "command",
        "id": str(command.id),
        "command": command.type,
        "payload": command.payload,
        "expires_at": _as_utc(command.expires_at).isoformat(),
    }


async def send_command(
    db: AsyncSession,
    agent_id: uuid.UUID,
    command_type: CommandType,
    payload: dict[str, Any] | None = None,
) -> AgentCommand:
    """Queue a command and push it now if the agent is connected.

    If it isn't, the command waits and is delivered when the agent reconnects,
    unless it expires first.
    """
    now = _utcnow()
    await repository.expire_overdue_commands(db, agent_id, now)
    command = AgentCommand(
        agent_id=agent_id,
        type=command_type,
        payload=payload or {},
        status=CommandStatus.PENDING,
        expires_at=now + COMMAND_TTL[command_type],
        created_at=now,
    )
    await repository.add_command(db, command)
    if await manager.send(agent_id, command_message(command)):
        await repository.mark_commands_sent(db, [command.id], _utcnow())
    return command


async def take_open_commands(
    db: AsyncSession, agent_id: uuid.UUID
) -> list[AgentCommand]:
    """Return every unacked, unexpired command and mark them sent.

    Used when an agent (re)connects. Commands that were sent before but never
    acked are included again, so agents must handle a repeated command id.
    """
    now = _utcnow()
    await repository.expire_overdue_commands(db, agent_id, now)
    commands = await repository.list_open_commands(db, agent_id, now)
    await repository.mark_commands_sent(db, [c.id for c in commands], now)
    return commands


async def acknowledge_command(
    db: AsyncSession, agent_id: uuid.UUID, ack: AckMessage
) -> bool:
    status = CommandStatus.ACKED if ack.ok else CommandStatus.FAILED
    error = None if ack.ok else ack.error
    return await repository.finish_command(
        db, agent_id, ack.id, status, error, _utcnow()
    )


async def notify_config_changed(db: AsyncSession, agent_id: uuid.UUID) -> int:
    """Bump the agent's config version and tell it to re-fetch its config.

    Older undelivered config_changed commands are dropped: the agent only
    needs the latest one. Returns the new config version.
    Raises AgentNotFoundError if the agent does not exist.
    """
    version = await repository.bump_config_version(db, agent_id)
    if version is None:
        raise AgentNotFoundError
    await repository.expire_open_commands(db, agent_id, CommandType.CONFIG_CHANGED)
    await send_command(
        db, agent_id, CommandType.CONFIG_CHANGED, {"config_version": version}
    )
    return version
