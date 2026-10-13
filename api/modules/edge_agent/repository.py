"""Database access for the edge_agent module."""

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import delete, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from api.modules.edge_agent.models import (
    AgentCommand,
    AgentStatus,
    CommandStatus,
    EdgeAgent,
)

_OPEN_COMMAND = (CommandStatus.PENDING, CommandStatus.SENT)


async def commit(db: AsyncSession) -> None:
    try:
        await db.commit()
    except Exception:
        await db.rollback()
        raise


# ---------- agents ----------


async def add_agent(db: AsyncSession, agent: EdgeAgent) -> EdgeAgent:
    db.add(agent)
    await commit(db)
    return agent


async def get_agent(db: AsyncSession, agent_id: uuid.UUID) -> EdgeAgent | None:
    return await db.get(EdgeAgent, agent_id)


async def get_agent_for_owner(
    db: AsyncSession, owner_id: uuid.UUID, agent_id: uuid.UUID
) -> EdgeAgent | None:
    return await db.scalar(
        select(EdgeAgent).where(
            EdgeAgent.id == agent_id, EdgeAgent.owner_id == owner_id
        )
    )


async def list_agents_for_owner(
    db: AsyncSession, owner_id: uuid.UUID
) -> list[EdgeAgent]:
    result = await db.scalars(
        select(EdgeAgent)
        .where(EdgeAgent.owner_id == owner_id)
        .order_by(EdgeAgent.created_at)
    )
    return list(result)


async def get_active_agent_by_token_hash(
    db: AsyncSession, token_hash: str
) -> EdgeAgent | None:
    return await db.scalar(
        select(EdgeAgent).where(
            EdgeAgent.token_hash == token_hash,
            EdgeAgent.status == AgentStatus.ACTIVE,
        )
    )


async def get_agent_by_enrollment_hash(
    db: AsyncSession, enrollment_hash: str
) -> EdgeAgent | None:
    return await db.scalar(
        select(EdgeAgent).where(EdgeAgent.enrollment_token_hash == enrollment_hash)
    )


async def claim_enrollment(
    db: AsyncSession,
    agent_id: uuid.UUID,
    enrollment_hash: str,
    now: datetime,
    values: dict[str, Any],
) -> bool:
    """Use an enrollment token and store the new agent token, without committing.

    Returns False if the token expired or another request already used it.
    """
    result = await db.execute(
        update(EdgeAgent)
        .where(
            EdgeAgent.id == agent_id,
            EdgeAgent.enrollment_token_hash == enrollment_hash,
            EdgeAgent.enrollment_expires_at > now,
        )
        .values(enrollment_token_hash=None, enrollment_expires_at=None, **values)
        .execution_options(synchronize_session="fetch")
    )
    return result.rowcount == 1


async def delete_agent(db: AsyncSession, agent_id: uuid.UUID) -> None:
    """Delete an agent; its commands go with it (ON DELETE CASCADE)."""
    await db.execute(
        delete(EdgeAgent)
        .where(EdgeAgent.id == agent_id)
        .execution_options(synchronize_session="fetch")
    )
    await commit(db)


async def bump_config_version(db: AsyncSession, agent_id: uuid.UUID) -> int | None:
    """Atomically add one to the agent's config_version and return the new value.

    Returns None if the agent does not exist.
    """
    await db.execute(
        update(EdgeAgent)
        .where(EdgeAgent.id == agent_id)
        .values(config_version=EdgeAgent.config_version + 1)
        .execution_options(synchronize_session="fetch")
    )
    await commit(db)
    return await db.scalar(
        select(EdgeAgent.config_version).where(EdgeAgent.id == agent_id)
    )


# ---------- commands ----------


async def add_command(db: AsyncSession, command: AgentCommand) -> AgentCommand:
    db.add(command)
    await commit(db)
    return command


async def get_open_command(
    db: AsyncSession, agent_id: uuid.UUID, command_type: str, now: datetime
) -> AgentCommand | None:
    """The newest unacked, unexpired command of this type, if any."""
    return await db.scalar(
        select(AgentCommand)
        .where(
            AgentCommand.agent_id == agent_id,
            AgentCommand.type == command_type,
            AgentCommand.status.in_(_OPEN_COMMAND),
            AgentCommand.expires_at > now,
        )
        .order_by(AgentCommand.created_at.desc())
        .limit(1)
    )


async def expire_overdue_commands(
    db: AsyncSession, agent_id: uuid.UUID, now: datetime
) -> None:
    """Mark the agent's open commands whose time ran out as expired, no commit."""
    await db.execute(
        update(AgentCommand)
        .where(
            AgentCommand.agent_id == agent_id,
            AgentCommand.status.in_(_OPEN_COMMAND),
            AgentCommand.expires_at <= now,
        )
        .values(status=CommandStatus.EXPIRED)
        .execution_options(synchronize_session="fetch")
    )


async def list_open_commands(
    db: AsyncSession, agent_id: uuid.UUID, now: datetime
) -> list[AgentCommand]:
    """Unexpired commands the agent has not acked yet, oldest first."""
    result = await db.scalars(
        select(AgentCommand)
        .where(
            AgentCommand.agent_id == agent_id,
            AgentCommand.status.in_(_OPEN_COMMAND),
            AgentCommand.expires_at > now,
        )
        .order_by(AgentCommand.created_at)
    )
    return list(result)


async def mark_commands_sent(
    db: AsyncSession, command_ids: list[uuid.UUID], now: datetime
) -> None:
    """Move pending commands to sent. Leaves already-acked ones alone."""
    if command_ids:
        await db.execute(
            update(AgentCommand)
            .where(
                AgentCommand.id.in_(command_ids),
                AgentCommand.status == CommandStatus.PENDING,
            )
            .values(status=CommandStatus.SENT, sent_at=now)
            .execution_options(synchronize_session="fetch")
        )
    await commit(db)


async def finish_command(
    db: AsyncSession,
    agent_id: uuid.UUID,
    command_id: uuid.UUID,
    status: CommandStatus,
    error: str | None,
    now: datetime,
) -> bool:
    """Record the agent's ack. Returns False if unknown, not its own, or already done."""
    result = await db.execute(
        update(AgentCommand)
        .where(
            AgentCommand.id == command_id,
            AgentCommand.agent_id == agent_id,
            AgentCommand.status.in_(_OPEN_COMMAND),
            AgentCommand.expires_at > now,
        )
        .values(status=status, error=error, acked_at=now)
        .execution_options(synchronize_session="fetch")
    )
    await commit(db)
    return result.rowcount == 1


async def expire_open_commands(
    db: AsyncSession, agent_id: uuid.UUID, command_type: str | None = None
) -> None:
    """Cancel the agent's unacked commands (optionally only one type), no commit."""
    query = update(AgentCommand).where(
        AgentCommand.agent_id == agent_id,
        AgentCommand.status.in_(_OPEN_COMMAND),
    )
    if command_type is not None:
        query = query.where(AgentCommand.type == command_type)
    await db.execute(
        query.values(status=CommandStatus.EXPIRED).execution_options(
            synchronize_session="fetch"
        )
    )
