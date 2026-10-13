"""Open WebSocket connections from edge agents, kept in memory.

This only works while the app runs as a single process. With several workers
or servers, commands must be relayed between processes (e.g. Redis pub/sub);
the command queue itself is in the database, so nothing is lost meanwhile.
"""

import asyncio
import contextlib
import uuid
from dataclasses import dataclass, field
from typing import Any

from fastapi import WebSocket, WebSocketDisconnect

# Custom close codes (4000-4999 are free for applications).
CLOSE_UNAUTHORIZED = 4401  # bad or revoked token: don't retry with it
CLOSE_REPLACED = 4409  # the same agent opened a newer connection


@dataclass(eq=False)
class AgentConnection:
    """One agent's socket. Every send goes through `lock`, so messages never
    interleave and the handshake (hello + backlog) can't be overtaken."""

    websocket: WebSocket
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)

    async def send(self, message: dict[str, Any]) -> None:
        async with self.lock:
            await self.websocket.send_json(message)


class ConnectionManager:
    def __init__(self) -> None:
        self._connections: dict[uuid.UUID, AgentConnection] = {}

    async def register(self, agent_id: uuid.UUID, connection: AgentConnection) -> None:
        """Track this connection; an older one of the same agent is closed."""
        old = self._connections.get(agent_id)
        self._connections[agent_id] = connection
        if old is not None:
            await _close_quietly(old.websocket, CLOSE_REPLACED)

    def unregister(self, agent_id: uuid.UUID, connection: AgentConnection) -> None:
        # Only forget it if a newer connection hasn't already taken its place.
        if self._connections.get(agent_id) is connection:
            del self._connections[agent_id]

    def is_connected(self, agent_id: uuid.UUID) -> bool:
        return agent_id in self._connections

    async def send(self, agent_id: uuid.UUID, message: dict[str, Any]) -> bool:
        """Send a JSON message. Returns False if the agent is not connected."""
        connection = self._connections.get(agent_id)
        if connection is None:
            return False
        try:
            await connection.send(message)
        except (WebSocketDisconnect, RuntimeError, OSError):
            # The agent dropped without us noticing yet; it gets the command
            # again from the queue when it reconnects.
            self.unregister(agent_id, connection)
            return False
        return True

    async def disconnect(self, agent_id: uuid.UUID, code: int) -> None:
        connection = self._connections.pop(agent_id, None)
        if connection is not None:
            await _close_quietly(connection.websocket, code)


async def _close_quietly(websocket: WebSocket, code: int) -> None:
    # The socket may already be closing on the other side.
    with contextlib.suppress(Exception):
        await websocket.close(code=code)


manager = ConnectionManager()
