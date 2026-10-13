"""Tests for the cameras module: adding and adopting cameras, discovery,
the agent's config, and stream status."""

import uuid
from datetime import UTC, datetime, timedelta

from httpx import AsyncClient
from sqlalchemy import update
from sqlalchemy.ext.asyncio import AsyncSession

from api.modules.cameras import credentials
from api.modules.cameras.models import Camera
from api.modules.edge_agent.models import EdgeAgent
from tests.test_edge_agent import (
    Headers,
    bearer,
    commands_of,
    create_agent,
    enrolled_agent,
    make_user,
)

CAMERA = {
    "name": "Front Gate",
    "rtsp_url": "rtsp://192.168.1.20:554/stream1",
    "username": "admin",
    "password": "s3cret pass",
}
DEVICE = {
    "device_key": "aa:bb:cc:00:00:01",
    "ip_address": "192.168.1.20",
    "vendor": "Hikvision",
    "model": "DS-2CD2043",
    "streams": [
        "rtsp://192.168.1.20:554/Streaming/101",
        "rtsp://192.168.1.20:554/Streaming/102",
    ],
}


# ---------- helpers ----------


async def add_camera(
    client: AsyncClient, user: Headers, agent_id: str, **changes: object
) -> dict:
    response = await client.post(
        "/cameras", json={**CAMERA, "agent_id": agent_id, **changes}, headers=user
    )
    assert response.status_code == 201, response.text
    return response.json()


async def discover(client: AsyncClient, token: str, *devices: dict):
    return await client.post(
        "/agent/cameras/discovered",
        json={"devices": list(devices)},
        headers=bearer(token),
    )


async def discovered_camera(
    client: AsyncClient, user: Headers, token: str, device: dict = DEVICE
) -> dict:
    """Report one device and return it as the user sees it."""
    assert (await discover(client, token, device)).status_code == 200
    cameras = (await client.get("/cameras", headers=user)).json()
    return next(c for c in cameras if c["ip_address"] == device["ip_address"])


async def agent_config(client: AsyncClient, token: str) -> dict:
    response = await client.get("/agent/config", headers=bearer(token))
    assert response.status_code == 200, response.text
    return response.json()


async def config_commands(db_session: AsyncSession, agent_id: str) -> list[dict]:
    return [
        c.payload
        for c in await commands_of(db_session, agent_id)
        if c.type == "config_changed"
    ]


async def report_status(client: AsyncClient, token: str, *reports: dict):
    return await client.post(
        "/agent/cameras/status", json={"cameras": list(reports)}, headers=bearer(token)
    )


# ---------- adding cameras ----------


async def test_add_camera_encrypts_password_and_notifies_agent(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    user = await make_user(db_session)
    agent_id, _ = await enrolled_agent(client, user)

    body = await add_camera(client, user, agent_id)

    assert body["state"] == "active"
    assert body["has_password"] is True
    assert body["stream_path"].startswith("cam_")
    assert body["stream_state"] == "unknown"
    assert body["online"] is False
    assert "password" not in body
    camera = await db_session.get(Camera, uuid.UUID(body["id"]))
    assert CAMERA["password"] not in camera.password_encrypted
    assert credentials.decrypt(camera.password_encrypted) == CAMERA["password"]
    assert await config_commands(db_session, agent_id) == [{"config_version": 2}]


async def test_camera_endpoints_require_login(client: AsyncClient) -> None:
    assert (await client.get("/cameras")).status_code == 401
    assert (await client.post("/cameras", json=CAMERA)).status_code == 401


async def test_cameras_need_an_enrolled_agent(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    user = await make_user(db_session)
    pending_id = (await create_agent(client, user))["agent"]["id"]
    revoked_id, _ = await enrolled_agent(client, user)
    await client.post(f"/agents/{revoked_id}/revoke", headers=user)

    for agent_id, expected in (
        (pending_id, 409),
        (revoked_id, 409),
        (str(uuid.uuid4()), 404),
    ):
        response = await client.post(
            "/cameras", json={**CAMERA, "agent_id": agent_id}, headers=user
        )
        assert response.status_code == expected, agent_id


async def test_list_filters_by_agent_and_state(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    user = await make_user(db_session)
    first_id, first_token = await enrolled_agent(client, user)
    second_id, _ = await enrolled_agent(client, user)
    await add_camera(client, user, first_id)
    await add_camera(client, user, second_id, name="Back Door")
    await discover(client, first_token, DEVICE)

    def names(response) -> list:
        return sorted(str(c["name"]) for c in response.json())

    everything = await client.get("/cameras", headers=user)
    by_agent = await client.get(f"/cameras?agent_id={first_id}", headers=user)
    discovered = await client.get("/cameras?state=discovered", headers=user)
    bad_state = await client.get("/cameras?state=bogus", headers=user)

    assert len(everything.json()) == 3
    assert names(by_agent) == ["Front Gate", "None"]
    assert [c["state"] for c in discovered.json()] == ["discovered"]
    assert bad_state.status_code == 422


# ---------- agent config ----------


async def test_agent_config_has_only_active_cameras_with_real_password(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    user = await make_user(db_session)
    agent_id, token = await enrolled_agent(client, user)
    active = await add_camera(client, user, agent_id, record_enabled=True)
    disabled = await add_camera(client, user, agent_id, name="Garage")
    await client.patch(
        f"/cameras/{disabled['id']}", json={"enabled": False}, headers=user
    )
    await discover(client, token, DEVICE)

    config = await agent_config(client, token)

    assert config["config_version"] == 4  # two adds and one disable
    assert config["cameras"] == [
        {
            "id": active["id"],
            "stream_path": active["stream_path"],
            "rtsp_url": CAMERA["rtsp_url"],
            "username": "admin",
            "password": CAMERA["password"],
            "record": True,
            "detection": False,
        }
    ]


async def test_camera_without_credentials(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    user = await make_user(db_session)
    agent_id, token = await enrolled_agent(client, user)
    body = await add_camera(client, user, agent_id, username=None, password=None)

    assert body["has_password"] is False
    [camera] = (await agent_config(client, token))["cameras"]
    assert camera["username"] is None
    assert camera["password"] is None


# ---------- discovery ----------


async def test_discovery_creates_then_updates_without_duplicates(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    user = await make_user(db_session)
    agent_id, token = await enrolled_agent(client, user)
    other = {**DEVICE, "device_key": "aa:bb:cc:00:00:02", "ip_address": "10.0.0.9"}

    first = await discover(client, token, DEVICE, other)
    moved = {**DEVICE, "ip_address": "192.168.1.77", "streams": []}
    second = await discover(client, token, moved)

    assert first.json() == {"new": 2, "updated": 0, "skipped": 0}
    assert second.json() == {"new": 0, "updated": 1, "skipped": 0}
    cameras = (await client.get("/cameras", headers=user)).json()
    assert len(cameras) == 2
    camera = next(c for c in cameras if c["vendor"] and c["ip_address"] != "10.0.0.9")
    assert camera["ip_address"] == "192.168.1.77"
    assert camera["discovered_streams"] == []
    assert camera["state"] == "discovered"
    assert camera["name"] is None
    assert camera["last_discovered_at"] is not None
    # Discovery alone changes nothing the agent streams.
    assert await config_commands(db_session, agent_id) == []


async def test_rescan_keeps_settings_of_adopted_camera(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    user = await make_user(db_session)
    _, token = await enrolled_agent(client, user)
    camera = await discovered_camera(client, user, token)
    await client.post(
        f"/cameras/{camera['id']}/adopt", json={"name": "Lobby"}, headers=user
    )

    await discover(client, token, {**DEVICE, "ip_address": "192.168.1.50"})

    after = (await client.get(f"/cameras/{camera['id']}", headers=user)).json()
    assert after["state"] == "active"
    assert after["name"] == "Lobby"
    assert after["rtsp_url"] == DEVICE["streams"][0]
    assert after["ip_address"] == "192.168.1.50"


# ---------- adopt and ignore ----------


async def test_adopt_uses_first_discovered_stream_by_default(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    user = await make_user(db_session)
    agent_id, token = await enrolled_agent(client, user)
    camera = await discovered_camera(client, user, token)

    adopted = await client.post(
        f"/cameras/{camera['id']}/adopt",
        json={"name": "Lobby", "username": "admin", "password": "pw"},
        headers=user,
    )
    again = await client.post(
        f"/cameras/{camera['id']}/adopt", json={"name": "Lobby"}, headers=user
    )

    assert adopted.status_code == 200, adopted.text
    assert adopted.json()["state"] == "active"
    assert adopted.json()["rtsp_url"] == DEVICE["streams"][0]
    assert again.status_code == 409
    [config_camera] = (await agent_config(client, token))["cameras"]
    assert config_camera["password"] == "pw"
    assert await config_commands(db_session, agent_id) == [{"config_version": 2}]


async def test_adopt_with_chosen_stream_or_none_found(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    user = await make_user(db_session)
    _, token = await enrolled_agent(client, user)
    with_streams = await discovered_camera(client, user, token)
    no_streams = await discovered_camera(
        client,
        user,
        token,
        {"device_key": "no-streams", "ip_address": "192.168.1.30"},
    )
    url = "rtsp://192.168.1.30/live"

    chosen = await client.post(
        f"/cameras/{with_streams['id']}/adopt",
        json={"name": "A", "rtsp_url": DEVICE["streams"][1]},
        headers=user,
    )
    missing = await client.post(
        f"/cameras/{no_streams['id']}/adopt", json={"name": "B"}, headers=user
    )
    given = await client.post(
        f"/cameras/{no_streams['id']}/adopt",
        json={"name": "B", "rtsp_url": url},
        headers=user,
    )

    assert chosen.json()["rtsp_url"] == DEVICE["streams"][1]
    assert missing.status_code == 422
    assert given.status_code == 200
    assert given.json()["rtsp_url"] == url


async def test_ignored_camera_stays_ignored_but_can_still_be_adopted(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    user = await make_user(db_session)
    _, token = await enrolled_agent(client, user)
    camera = await discovered_camera(client, user, token)

    ignored = await client.post(f"/cameras/{camera['id']}/ignore", headers=user)
    await discover(client, token, DEVICE)
    after_rescan = await client.get(f"/cameras/{camera['id']}", headers=user)
    ignore_again = await client.post(f"/cameras/{camera['id']}/ignore", headers=user)
    adopted = await client.post(
        f"/cameras/{camera['id']}/adopt", json={"name": "Yard"}, headers=user
    )

    assert ignored.json()["state"] == "ignored"
    assert after_rescan.json()["state"] == "ignored"
    assert ignore_again.status_code == 409
    assert adopted.json()["state"] == "active"


# ---------- updating and deleting ----------


async def test_update_only_notifies_agent_for_what_it_uses(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    user = await make_user(db_session)
    agent_id, token = await enrolled_agent(client, user)
    camera = await add_camera(client, user, agent_id)
    url = f"/cameras/{camera['id']}"

    renamed = await client.patch(url, json={"name": "  Gate  "}, headers=user)
    assert renamed.json()["name"] == "Gate"
    assert len(await config_commands(db_session, agent_id)) == 1

    await client.patch(url, json={"detection_enabled": True}, headers=user)
    cleared = await client.patch(
        url, json={"username": None, "password": None}, headers=user
    )
    assert cleared.json()["has_password"] is False
    assert cleared.json()["username"] is None
    [config_camera] = (await agent_config(client, token))["cameras"]
    assert config_camera["detection"] is True
    assert config_camera["password"] is None
    assert len(await config_commands(db_session, agent_id)) == 3


async def test_disable_and_enable(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    user = await make_user(db_session)
    agent_id, token = await enrolled_agent(client, user)
    camera = await add_camera(client, user, agent_id)
    url = f"/cameras/{camera['id']}"

    disabled = await client.patch(url, json={"enabled": False}, headers=user)
    assert disabled.json()["state"] == "disabled"
    assert (await agent_config(client, token))["cameras"] == []

    # Changing a disabled camera is nothing the agent needs to hear about.
    await client.patch(url, json={"rtsp_url": "rtsp://10.0.0.5/x"}, headers=user)
    assert len(await config_commands(db_session, agent_id)) == 2

    enabled = await client.patch(url, json={"enabled": True}, headers=user)
    assert enabled.json()["state"] == "active"
    [config_camera] = (await agent_config(client, token))["cameras"]
    assert config_camera["rtsp_url"] == "rtsp://10.0.0.5/x"


async def test_discovered_camera_must_be_adopted_before_patch(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    user = await make_user(db_session)
    _, token = await enrolled_agent(client, user)
    camera = await discovered_camera(client, user, token)

    response = await client.patch(
        f"/cameras/{camera['id']}", json={"name": "X"}, headers=user
    )

    assert response.status_code == 409


async def test_delete_camera(client: AsyncClient, db_session: AsyncSession) -> None:
    user = await make_user(db_session)
    agent_id, token = await enrolled_agent(client, user)
    active = await add_camera(client, user, agent_id)
    discovered = await discovered_camera(client, user, token)

    assert (
        await client.delete(f"/cameras/{active['id']}", headers=user)
    ).status_code == 204
    assert (
        await client.delete(f"/cameras/{discovered['id']}", headers=user)
    ).status_code == 204

    assert (
        await client.get(f"/cameras/{active['id']}", headers=user)
    ).status_code == 404
    assert (await agent_config(client, token))["cameras"] == []
    # One for the add, one for deleting the active camera; none for discovered.
    assert len(await config_commands(db_session, agent_id)) == 2


async def test_deleting_agent_deletes_its_cameras(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    user = await make_user(db_session)
    agent_id, token = await enrolled_agent(client, user)
    await add_camera(client, user, agent_id)
    await discover(client, token, DEVICE)

    await client.delete(f"/agents/{agent_id}", headers=user)

    assert (await client.get("/cameras", headers=user)).json() == []


# ---------- stream status ----------


async def test_status_report_makes_camera_online(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    user = await make_user(db_session)
    agent_id, token = await enrolled_agent(client, user)
    camera = await add_camera(client, user, agent_id)
    url = f"/cameras/{camera['id']}"

    report = await report_status(
        client, token, {"camera_id": camera["id"], "state": "streaming"}
    )

    assert report.json() == {"updated": 1}
    body = (await client.get(url, headers=user)).json()
    assert body["online"] is True
    assert body["stream_state"] == "streaming"
    assert body["stream_checked_at"] is not None


async def test_camera_goes_offline_on_old_report_offline_agent_or_error(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    user = await make_user(db_session)
    agent_id, token = await enrolled_agent(client, user)
    camera = await add_camera(client, user, agent_id)
    url = f"/cameras/{camera['id']}"
    streaming = {"camera_id": camera["id"], "state": "streaming"}
    long_ago = datetime.now(UTC) - timedelta(minutes=5)

    await report_status(client, token, streaming)
    await db_session.execute(
        update(Camera)
        .where(Camera.id == uuid.UUID(camera["id"]))
        .values(stream_checked_at=long_ago)
    )
    await db_session.commit()
    assert (await client.get(url, headers=user)).json()["online"] is False

    await report_status(client, token, streaming)
    await db_session.execute(
        update(EdgeAgent)
        .where(EdgeAgent.id == uuid.UUID(agent_id))
        .values(last_seen_at=long_ago)
    )
    await db_session.commit()
    assert (await client.get(url, headers=user)).json()["online"] is False

    await report_status(
        client,
        token,
        {"camera_id": camera["id"], "state": "error", "error": "401 Unauthorized"},
    )
    body = (await client.get(url, headers=user)).json()
    assert body["stream_state"] == "error"
    assert body["stream_error"] == "401 Unauthorized"
    assert body["online"] is False


async def test_changing_stream_settings_resets_status(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    user = await make_user(db_session)
    agent_id, token = await enrolled_agent(client, user)
    camera = await add_camera(client, user, agent_id)
    url = f"/cameras/{camera['id']}"
    await report_status(
        client, token, {"camera_id": camera["id"], "state": "streaming"}
    )

    renamed = await client.patch(url, json={"name": "Gate 2"}, headers=user)
    assert renamed.json()["stream_state"] == "streaming"

    new_password = await client.patch(url, json={"password": "new"}, headers=user)
    assert new_password.json()["stream_state"] == "unknown"
    assert new_password.json()["stream_checked_at"] is None
    assert new_password.json()["online"] is False
