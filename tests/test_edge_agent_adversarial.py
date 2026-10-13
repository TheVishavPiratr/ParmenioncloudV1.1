"""Hostile and edge-case tests for the edge_agent module: bad input, agents
poking at each other's data, token confusion, and protocol ordering."""

import asyncio
import uuid
from datetime import UTC, datetime

import pytest
from httpx import AsyncClient
from httpx import AsyncClient as Client
from httpx_ws import WebSocketDisconnect, aconnect_ws
from httpx_ws.transport import ASGIWebSocketTransport
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from api.main import app
from api.modules.auth.models import User
from api.modules.edge_agent import service
from api.modules.edge_agent.connections import manager
from api.modules.edge_agent.models import AgentCommand
from tests.test_edge_agent import (
    ENROLL,
    HEARTBEAT,
    agent_socket,
    bearer,
    commands_of,
    create_agent,
    enroll,
    enrolled_agent,
    make_user,
    receive,
)

# ---------- hostile input ----------


@pytest.mark.parametrize("field", ["hostname", "platform", "software_version"])
async def test_enroll_rejects_nul_bytes(
    client: AsyncClient, db_session: AsyncSession, field: str
) -> None:
    user = await make_user(db_session)
    created = await create_agent(client, user)

    response = await client.post(
        "/agent/enroll",
        json={
            **ENROLL,
            field: "a\x00b",
            "enrollment_token": created["enrollment_token"],
        },
    )

    assert response.status_code == 422
    # The token was not burned by the rejected request.
    assert (await enroll(client, created["enrollment_token"])).status_code == 200


async def test_names_and_heartbeat_reject_nul_bytes(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    user = await make_user(db_session)
    agent_id, token = await enrolled_agent(client, user)

    create = await client.post("/agents", json={"name": "a\x00b"}, headers=user)
    rename = await client.patch(
        f"/agents/{agent_id}", json={"name": "a\x00b"}, headers=user
    )
    heartbeat = await client.post(
        "/agent/heartbeat",
        json={**HEARTBEAT, "software_version": "1\x00"},
        headers=bearer(token),
    )
    mediamtx = await client.post(
        "/agent/heartbeat",
        json={**HEARTBEAT, "mediamtx": {"running": True, "version": "\x00"}},
        headers=bearer(token),
    )

    assert [r.status_code for r in (create, rename, heartbeat, mediamtx)] == [422] * 4


async def test_enrollment_token_with_nul_byte_is_just_invalid(
    client: AsyncClient,
) -> None:
    response = await client.post(
        "/agent/enroll", json={**ENROLL, "enrollment_token": "vpe_\x00"}
    )
    assert response.status_code in (400, 422)


async def test_unicode_names_round_trip(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    user = await make_user(db_session)
    body = await create_agent(client, user, "Café 東京 🎥")
    assert body["agent"]["name"] == "Café 東京 🎥"


@pytest.mark.parametrize(
    "health",
    [
        {"cpu_pct": -0.1},
        {"mem_pct": 100.01},
        {"uptime_s": 1.5},
        {"uptime_s": "ten"},
        {"mediamtx": {"running": "maybe"}},
        {"mediamtx": {"running": True, "active_paths": -1}},
        {"software_version": ""},
        {"software_version": "x" * 65},
    ],
)
async def test_heartbeat_rejects_bad_values(
    client: AsyncClient, db_session: AsyncSession, health: dict
) -> None:
    user = await make_user(db_session)
    _, token = await enrolled_agent(client, user)
    response = await client.post(
        "/agent/heartbeat", json={**HEARTBEAT, **health}, headers=bearer(token)
    )
    assert response.status_code == 422


async def test_heartbeat_accepts_boundaries(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    user = await make_user(db_session)
    _, token = await enrolled_agent(client, user)
    response = await client.post(
        "/agent/heartbeat",
        json={
            **HEARTBEAT,
            "cpu_pct": 0,
            "mem_pct": 100,
            "disk_pct": None,
            "uptime_s": 2**62,
            "mediamtx": {"running": False},
        },
        headers=bearer(token),
    )
    assert response.status_code == 200


# ---------- token confusion ----------


async def test_agent_token_is_not_a_user_token(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    user = await make_user(db_session)
    _, token = await enrolled_agent(client, user)
    assert (await client.get("/agents", headers=bearer(token))).status_code == 401
    assert (await client.get("/auth/me", headers=bearer(token))).status_code == 401


async def test_websocket_rejects_enrollment_token_user_token_and_no_header(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    user = await make_user(db_session)
    created = await create_agent(client, user)
    user_token = user["Authorization"].split()[1]

    for token in [created["enrollment_token"], user_token]:
        async with agent_socket(token) as ws:
            with pytest.raises(WebSocketDisconnect) as closed:
                await receive(ws)
        assert closed.value.code == 4401

    async with (
        Client(transport=ASGIWebSocketTransport(app=app), base_url="http://test") as c,
        aconnect_ws("http://test/agent/ws", c) as ws,
    ):
        with pytest.raises(WebSocketDisconnect) as closed:
            await receive(ws)
    assert closed.value.code == 4401


async def test_websocket_accepts_lowercase_scheme_and_extra_spaces(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    user = await make_user(db_session)
    _, token = await enrolled_agent(client, user)
    for header in [f"bearer {token}", f"Bearer   {token}"]:
        transport = ASGIWebSocketTransport(app=app)
        async with (
            Client(transport=transport, base_url="http://test") as ws_client,
            aconnect_ws(
                "http://test/agent/ws", ws_client, headers={"Authorization": header}
            ) as ws,
        ):
            assert (await receive(ws))["type"] == "hello"


async def test_deleted_agent_token_is_dead_and_socket_closed(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    user = await make_user(db_session)
    agent_id, token = await enrolled_agent(client, user)

    async with agent_socket(token) as ws:
        await receive(ws)  # hello
        await client.delete(f"/agents/{agent_id}", headers=user)
        with pytest.raises(WebSocketDisconnect) as closed:
            await receive(ws)
    assert closed.value.code == 4401
    assert (await client.get("/agent/me", headers=bearer(token))).status_code == 401


async def test_disabled_owner_disables_their_agents(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    user = await make_user(db_session)
    _, token = await enrolled_agent(client, user)
    await db_session.execute(update(User).values(is_active=False))
    await db_session.commit()

    assert (await client.get("/agent/me", headers=bearer(token))).status_code == 401
    async with agent_socket(token) as ws:
        with pytest.raises(WebSocketDisconnect) as closed:
            await receive(ws)
    assert closed.value.code == 4401


# ---------- cross-agent isolation ----------


async def test_agent_cannot_ack_another_agents_command(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    user = await make_user(db_session)
    victim_id, _ = await enrolled_agent(client, user)
    _, attacker_token = await enrolled_agent(client, user)
    command_id = (
        await client.post(f"/agents/{victim_id}/rescan", headers=user)
    ).json()["command_id"]

    async with agent_socket(attacker_token) as ws:
        await receive(ws)  # hello: the attacker gets none of the victim's commands
        await ws.send_json({"type": "ack", "id": command_id, "ok": False, "error": "x"})
        await receive(ws)  # reply

    [command] = await commands_of(db_session, victim_id)
    assert command.status == "pending"
    assert command.error is None


# ---------- command lifecycle ----------


async def test_time_expired_commands_are_marked_expired(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    """Design: status 'expired' = never acked in time. A command past its
    expires_at must not stay 'pending'/'sent' forever, nor accept a late ack."""
    user = await make_user(db_session)
    agent_id, token = await enrolled_agent(client, user)
    command_id = (await client.post(f"/agents/{agent_id}/rescan", headers=user)).json()[
        "command_id"
    ]
    await db_session.execute(update(AgentCommand).values(expires_at=datetime.now(UTC)))
    await db_session.commit()

    async with agent_socket(token) as ws:
        await receive(ws)  # hello
        await ws.send_json({"type": "ack", "id": command_id, "ok": True})
        await receive(ws)

    [command] = await commands_of(db_session, agent_id)
    assert command.status == "expired"


async def test_hello_is_always_first_even_if_command_races_the_connect(
    client: AsyncClient, db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A command pushed while the socket is being set up must not overtake hello."""
    user = await make_user(db_session)
    _, token = await enrolled_agent(client, user)
    original = service.take_open_commands
    racers: list[asyncio.Task] = []

    async def racing_take_open_commands(db, agent_id):
        # Another request pushes a command at the exact moment the agent connects.
        racer = {"type": "command", "id": "racer", "command": "rescan_cameras"}
        racers.append(asyncio.create_task(manager.send(agent_id, racer)))
        await asyncio.sleep(0)  # let it run until it has to wait
        return await original(db, agent_id)

    monkeypatch.setattr(service, "take_open_commands", racing_take_open_commands)

    async with agent_socket(token) as ws:
        assert (await receive(ws))["type"] == "hello"
        assert (await receive(ws))["id"] == "racer"
    assert await racers[0] is True


async def test_repeated_rescan_does_not_pile_up_commands(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    """Tapping Rescan 50 times while the agent is offline should not queue 50
    scans to run back to back when it reconnects."""
    user = await make_user(db_session)
    agent_id, _ = await enrolled_agent(client, user)

    ids = {
        (await client.post(f"/agents/{agent_id}/rescan", headers=user)).json()[
            "command_id"
        ]
        for _ in range(50)
    }

    open_commands = [
        c for c in await commands_of(db_session, agent_id) if c.status == "pending"
    ]
    assert len(open_commands) == 1
    assert len(ids) == 1


async def test_notify_config_changed_for_missing_agent_raises_clean_error(
    db_session: AsyncSession,
) -> None:
    with pytest.raises(service.AgentNotFoundError):
        await service.notify_config_changed(db_session, uuid.uuid4())


async def test_huge_and_binary_websocket_messages_are_survivable(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    user = await make_user(db_session)
    _, token = await enrolled_agent(client, user)

    async with agent_socket(token) as ws:
        await receive(ws)  # hello
        await ws.send_bytes(b"\xff\xfe garbage")
        assert (await receive(ws))["type"] == "error"
        await ws.send_json(
            {"type": "ack", "id": str(uuid.uuid4()), "ok": False, "error": "e" * 501}
        )
        assert (await receive(ws))["type"] == "error"
        await ws.send_json({"type": "ack", "id": "not-a-uuid", "ok": True})
        assert (await receive(ws))["type"] == "error"
        await ws.send_json([1, 2, 3])
        assert (await receive(ws))["type"] == "error"
        # Still alive afterwards.
        good = str(uuid.uuid4())
        await ws.send_json({"type": "ack", "id": good, "ok": True})
        assert await receive(ws) == {"type": "ack_received", "id": good}


async def test_revoked_pending_agent_cannot_be_rescanned_or_heartbeat(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    user = await make_user(db_session)
    agent_id, token = await enrolled_agent(client, user)
    await client.post(f"/agents/{agent_id}/revoke", headers=user)

    assert (
        await client.post(f"/agents/{agent_id}/rescan", headers=user)
    ).status_code == 409
    response = await client.post(
        "/agent/heartbeat", json=HEARTBEAT, headers=bearer(token)
    )
    assert response.status_code == 401
    # Revoking twice is harmless.
    again = await client.post(f"/agents/{agent_id}/revoke", headers=user)
    assert again.status_code == 200
    assert (
        await db_session.scalar(
            select(AgentCommand.id).where(AgentCommand.agent_id == uuid.UUID(agent_id))
        )
    ) is None
