"""Tests for the edge_agent module: agent management, enrollment, heartbeat,
and the WebSocket command channel."""

import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta

import pytest
from httpx import AsyncClient
from httpx_ws import AsyncWebSocketSession, WebSocketDisconnect, aconnect_ws
from httpx_ws.transport import ASGIWebSocketTransport
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from api.main import app
from api.modules.auth import service as auth_service
from api.modules.auth.models import User
from api.modules.edge_agent import service
from api.modules.edge_agent.models import AgentCommand, EdgeAgent

Headers = dict[str, str]

HEARTBEAT = {
    "software_version": "1.0.0",
    "uptime_s": 120,
    "cpu_pct": 12.5,
    "mem_pct": 40,
    "disk_pct": 55,
    "mediamtx": {"running": True, "version": "1.9.0", "active_paths": 2},
}
ENROLL = {
    "hostname": "edge-box",
    "platform": "linux-arm64",
    "software_version": "1.0.0",
}


# ---------- helpers ----------


async def make_user(
    db_session: AsyncSession, email: str = "owner@example.com"
) -> Headers:
    user = User(
        email=email,
        password_hash="unused",
        full_name="Owner",
        is_active=True,
        is_verified=True,
    )
    db_session.add(user)
    await db_session.commit()
    return {"Authorization": f"Bearer {auth_service.create_access_token(user.id)}"}


def bearer(token: str) -> Headers:
    return {"Authorization": f"Bearer {token}"}


async def create_agent(client: AsyncClient, user: Headers, name: str = "Main Office"):
    response = await client.post("/agents", json={"name": name}, headers=user)
    assert response.status_code == 201, response.text
    return response.json()


async def enroll(client: AsyncClient, enrollment_token: str):
    return await client.post(
        "/agent/enroll", json={**ENROLL, "enrollment_token": enrollment_token}
    )


async def enrolled_agent(client: AsyncClient, user: Headers) -> tuple[str, str]:
    """Create and enroll an agent. Returns (agent_id, agent_token)."""
    created = await create_agent(client, user)
    body = (await enroll(client, created["enrollment_token"])).json()
    return body["agent_id"], body["agent_token"]


@asynccontextmanager
async def agent_socket(token: str) -> AsyncIterator[AsyncWebSocketSession]:
    """Open the agent WebSocket. Its transport must be entered and exited in the
    test's own task, so it gets a client of its own (same app, same db_session)."""
    transport = ASGIWebSocketTransport(app=app)
    async with (
        AsyncClient(transport=transport, base_url="http://test") as ws_client,
        aconnect_ws("http://test/agent/ws", ws_client, headers=bearer(token)) as ws,
    ):
        yield ws


async def receive(ws: AsyncWebSocketSession) -> dict:
    return await ws.receive_json(timeout=2)


async def commands_of(db_session: AsyncSession, agent_id: str) -> list[AgentCommand]:
    result = await db_session.scalars(
        select(AgentCommand)
        .where(AgentCommand.agent_id == uuid.UUID(agent_id))
        .order_by(AgentCommand.created_at)
    )
    return list(result)


# ---------- creating and managing agents ----------


async def test_create_agent_returns_one_time_enrollment_token(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    user = await make_user(db_session)

    body = await create_agent(client, user, "  Main Office  ")

    assert body["enrollment_token"].startswith("vpe_")
    assert body["agent"]["name"] == "Main Office"
    assert body["agent"]["status"] == "pending"
    assert body["agent"]["online"] is False
    assert "token_hash" not in body["agent"]
    agent = await db_session.get(EdgeAgent, uuid.UUID(body["agent"]["id"]))
    assert body["enrollment_token"] not in agent.enrollment_token_hash


async def test_agent_endpoints_require_login(client: AsyncClient) -> None:
    assert (await client.get("/agents")).status_code == 401
    assert (await client.post("/agents", json={"name": "x"})).status_code == 401


async def test_create_agent_validates_name(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    user = await make_user(db_session)
    blank = await client.post("/agents", json={"name": "   "}, headers=user)
    too_long = await client.post("/agents", json={"name": "x" * 101}, headers=user)
    assert blank.status_code == too_long.status_code == 422


async def test_users_only_see_their_own_agents(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    alice = await make_user(db_session, "alice@example.com")
    bob = await make_user(db_session, "bob@example.com")
    agent_id = (await create_agent(client, alice))["agent"]["id"]

    assert [a["id"] for a in (await client.get("/agents", headers=alice)).json()] == [
        agent_id
    ]
    assert (await client.get("/agents", headers=bob)).json() == []
    # Someone else's agent looks exactly like a missing one.
    for response in [
        await client.get(f"/agents/{agent_id}", headers=bob),
        await client.patch(f"/agents/{agent_id}", json={"name": "x"}, headers=bob),
        await client.post(f"/agents/{agent_id}/revoke", headers=bob),
        await client.post(f"/agents/{agent_id}/rescan", headers=bob),
        await client.post(f"/agents/{agent_id}/enrollment-token", headers=bob),
        await client.delete(f"/agents/{agent_id}", headers=bob),
        await client.get(f"/agents/{uuid.uuid4()}", headers=alice),
    ]:
        assert response.status_code == 404
        assert response.json() == {"detail": "Agent not found"}


async def test_rename_and_delete_agent(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    user = await make_user(db_session)
    agent_id = (await create_agent(client, user))["agent"]["id"]

    renamed = await client.patch(
        f"/agents/{agent_id}", json={"name": "Warehouse"}, headers=user
    )
    assert renamed.status_code == 200
    assert renamed.json()["name"] == "Warehouse"

    assert (await client.delete(f"/agents/{agent_id}", headers=user)).status_code == 204
    assert (await client.get(f"/agents/{agent_id}", headers=user)).status_code == 404


# ---------- enrollment ----------


async def test_enroll_activates_agent_and_token_is_single_use(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    user = await make_user(db_session)
    created = await create_agent(client, user)

    first = await enroll(client, created["enrollment_token"])
    second = await enroll(client, created["enrollment_token"])

    assert first.status_code == 200
    body = first.json()
    assert body["agent_id"] == created["agent"]["id"]
    assert body["agent_token"].startswith("vpa_")
    assert body["heartbeat_interval_seconds"] == 30
    assert second.status_code == 400

    agent = (await client.get(f"/agents/{body['agent_id']}", headers=user)).json()
    assert agent["status"] == "active"
    assert agent["online"] is True
    assert agent["hostname"] == "edge-box"
    assert agent["platform"] == "linux-arm64"


async def test_enroll_rejects_bad_and_expired_tokens(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    user = await make_user(db_session)
    created = await create_agent(client, user)
    await db_session.execute(
        update(EdgeAgent).values(enrollment_expires_at=datetime.now(UTC))
    )
    await db_session.commit()

    for token in [created["enrollment_token"], "vpe_made-up", "not-a-token"]:
        response = await enroll(client, token)
        assert response.status_code == 400
        assert response.json() == {"detail": "Invalid or expired enrollment token"}


async def test_new_enrollment_token_replaces_box(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    user = await make_user(db_session)
    created = await create_agent(client, user)
    old_box = (await enroll(client, created["enrollment_token"])).json()["agent_token"]

    fresh = await client.post(
        f"/agents/{created['agent']['id']}/enrollment-token", headers=user
    )
    assert fresh.status_code == 201
    # The current box keeps working until the new token is used.
    assert (await client.get("/agent/me", headers=bearer(old_box))).status_code == 200

    new_box = (await enroll(client, fresh.json()["enrollment_token"])).json()
    assert new_box["agent_id"] == created["agent"]["id"]
    assert (await client.get("/agent/me", headers=bearer(old_box))).status_code == 401
    me = await client.get("/agent/me", headers=bearer(new_box["agent_token"]))
    assert me.status_code == 200


# ---------- agent token, heartbeat, online status ----------


async def test_agent_endpoints_reject_bad_tokens(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    user = await make_user(db_session)
    created = await create_agent(client, user)

    for headers in [
        {},
        bearer("vpa_made-up"),
        bearer(created["enrollment_token"]),  # not an agent token
        await make_user(db_session, "someone@example.com"),  # a user JWT
    ]:
        assert (await client.get("/agent/me", headers=headers)).status_code == 401
        response = await client.post(
            "/agent/heartbeat", json=HEARTBEAT, headers=headers
        )
        assert response.status_code == 401


async def test_heartbeat_records_health(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    user = await make_user(db_session)
    agent_id, token = await enrolled_agent(client, user)

    response = await client.post(
        "/agent/heartbeat",
        json={**HEARTBEAT, "software_version": "1.0.1"},
        headers=bearer(token),
    )

    assert response.status_code == 200
    assert response.json()["config_version"] == 1
    assert response.json()["heartbeat_interval_seconds"] == 30
    agent = (await client.get(f"/agents/{agent_id}", headers=user)).json()
    assert agent["online"] is True
    assert agent["software_version"] == "1.0.1"
    assert agent["mediamtx"] == {"running": True, "version": "1.9.0"}
    assert agent["health"] == {
        "uptime_s": 120,
        "cpu_pct": 12.5,
        "mem_pct": 40.0,
        "disk_pct": 55.0,
        "mediamtx_active_paths": 2,
    }


async def test_heartbeat_validates_input(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    user = await make_user(db_session)
    _, token = await enrolled_agent(client, user)

    for body in [
        {**HEARTBEAT, "cpu_pct": 101},
        {**HEARTBEAT, "uptime_s": -1},
        {k: v for k, v in HEARTBEAT.items() if k != "mediamtx"},
    ]:
        response = await client.post(
            "/agent/heartbeat", json=body, headers=bearer(token)
        )
        assert response.status_code == 422


async def test_agent_goes_offline_after_missed_heartbeats(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    user = await make_user(db_session)
    agent_id, _ = await enrolled_agent(client, user)
    await db_session.execute(
        update(EdgeAgent).values(last_seen_at=datetime.now(UTC) - timedelta(seconds=91))
    )
    await db_session.commit()

    agent = (await client.get(f"/agents/{agent_id}", headers=user)).json()
    assert agent["status"] == "active"
    assert agent["online"] is False


# ---------- revoke ----------


async def test_revoke_kills_agent_and_enrollment_tokens(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    user = await make_user(db_session)
    agent_id, token = await enrolled_agent(client, user)
    spare = await client.post(f"/agents/{agent_id}/enrollment-token", headers=user)

    revoked = await client.post(f"/agents/{agent_id}/revoke", headers=user)

    assert revoked.status_code == 200
    assert revoked.json()["status"] == "revoked"
    assert revoked.json()["online"] is False
    assert (await client.get("/agent/me", headers=bearer(token))).status_code == 401
    assert (await enroll(client, spare.json()["enrollment_token"])).status_code == 400

    # The owner can bring the agent back with a new enrollment token.
    fresh = await client.post(f"/agents/{agent_id}/enrollment-token", headers=user)
    assert (await enroll(client, fresh.json()["enrollment_token"])).status_code == 200
    agent = (await client.get(f"/agents/{agent_id}", headers=user)).json()
    assert agent["status"] == "active"


# ---------- commands and the WebSocket ----------


async def test_rescan_requires_active_agent(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    user = await make_user(db_session)
    agent_id = (await create_agent(client, user))["agent"]["id"]

    response = await client.post(f"/agents/{agent_id}/rescan", headers=user)

    assert response.status_code == 409


async def test_websocket_rejects_bad_token() -> None:
    async with agent_socket("vpa_made-up") as ws:
        with pytest.raises(WebSocketDisconnect) as closed:
            await receive(ws)
    assert closed.value.code == 4401


async def test_command_queued_offline_is_delivered_on_connect(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    user = await make_user(db_session)
    agent_id, token = await enrolled_agent(client, user)

    queued = await client.post(f"/agents/{agent_id}/rescan", headers=user)
    assert queued.status_code == 202
    command_id = queued.json()["command_id"]
    assert [c.status for c in await commands_of(db_session, agent_id)] == ["pending"]

    async with agent_socket(token) as ws:
        hello = await receive(ws)
        assert hello == {"type": "hello", "agent_id": agent_id, "config_version": 1}
        command = await receive(ws)
        assert command["type"] == "command"
        assert command["id"] == command_id
        assert command["command"] == "rescan_cameras"

        await ws.send_json({"type": "ack", "id": command_id, "ok": True})
        assert await receive(ws) == {"type": "ack_received", "id": command_id}

    [stored] = await commands_of(db_session, agent_id)
    assert stored.status == "acked"
    assert stored.acked_at is not None


async def test_command_is_pushed_live_and_failure_is_recorded(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    user = await make_user(db_session)
    agent_id, token = await enrolled_agent(client, user)

    async with agent_socket(token) as ws:
        await receive(ws)  # hello
        queued = await client.post(f"/agents/{agent_id}/rescan", headers=user)
        command = await receive(ws)
        assert command["id"] == queued.json()["command_id"]
        [stored] = await commands_of(db_session, agent_id)
        assert stored.status == "sent"

        await ws.send_json(
            {"type": "ack", "id": command["id"], "ok": False, "error": "scan failed"}
        )
        await receive(ws)  # ack_received

    [stored] = await commands_of(db_session, agent_id)
    assert stored.status == "failed"
    assert stored.error == "scan failed"


async def test_unacked_command_is_redelivered_but_expired_one_is_not(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    user = await make_user(db_session)
    agent_id, token = await enrolled_agent(client, user)
    stale = (await client.post(f"/agents/{agent_id}/rescan", headers=user)).json()
    await db_session.execute(
        update(AgentCommand)
        .where(AgentCommand.id == uuid.UUID(stale["command_id"]))
        .values(expires_at=datetime.now(UTC))
    )
    await db_session.commit()
    live = (await client.post(f"/agents/{agent_id}/rescan", headers=user)).json()

    for _ in range(2):  # connect, drop without acking, reconnect
        async with agent_socket(token) as ws:
            await receive(ws)  # hello
            assert (await receive(ws))["id"] == live["command_id"]
            await ws.send_json({"type": "ping"})
            # Nothing else was queued: the next message is the error reply.
            assert (await receive(ws))["type"] == "error"


async def test_invalid_message_gets_error_and_connection_stays_open(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    user = await make_user(db_session)
    _, token = await enrolled_agent(client, user)

    async with agent_socket(token) as ws:
        await receive(ws)  # hello
        await ws.send_text("not json")
        assert await receive(ws) == {
            "type": "error",
            "detail": "Expected an ack message",
        }
        # Acking an unknown command is harmless.
        unknown = str(uuid.uuid4())
        await ws.send_json({"type": "ack", "id": unknown, "ok": True})
        assert await receive(ws) == {"type": "ack_received", "id": unknown}


async def test_revoke_closes_open_websocket(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    user = await make_user(db_session)
    agent_id, token = await enrolled_agent(client, user)
    await client.post(f"/agents/{agent_id}/rescan", headers=user)

    async with agent_socket(token) as ws:
        await receive(ws)  # hello
        await receive(ws)  # rescan
        await client.post(f"/agents/{agent_id}/revoke", headers=user)
        with pytest.raises(WebSocketDisconnect) as closed:
            await receive(ws)
    assert closed.value.code == 4401
    assert [c.status for c in await commands_of(db_session, agent_id)] == ["expired"]


async def test_newer_connection_replaces_older_one(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    user = await make_user(db_session)
    agent_id, token = await enrolled_agent(client, user)

    async with agent_socket(token) as old:
        await receive(old)  # hello
        async with agent_socket(token) as new:
            await receive(new)  # hello
            with pytest.raises(WebSocketDisconnect) as closed:
                await receive(old)
            assert closed.value.code == 4409

            queued = await client.post(f"/agents/{agent_id}/rescan", headers=user)
            assert (await receive(new))["id"] == queued.json()["command_id"]


# ---------- config changes (called by other modules) ----------


async def test_notify_config_changed_bumps_version_and_keeps_only_latest(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    user = await make_user(db_session)
    agent_id, token = await enrolled_agent(client, user)

    # Two changes while the agent is offline: only the latest matters.
    await service.notify_config_changed(db_session, uuid.UUID(agent_id))
    assert await service.notify_config_changed(db_session, uuid.UUID(agent_id)) == 3

    heartbeat = await client.post(
        "/agent/heartbeat", json=HEARTBEAT, headers=bearer(token)
    )
    assert heartbeat.json()["config_version"] == 3

    async with agent_socket(token) as ws:
        assert (await receive(ws))["config_version"] == 3
        command = await receive(ws)
        assert command["command"] == "config_changed"
        assert command["payload"] == {"config_version": 3}

        # A change while connected is pushed straight away.
        await service.notify_config_changed(db_session, uuid.UUID(agent_id))
        assert (await receive(ws))["payload"] == {"config_version": 4}

    statuses = [c.status for c in await commands_of(db_session, agent_id)]
    assert statuses == ["expired", "expired", "sent"]
