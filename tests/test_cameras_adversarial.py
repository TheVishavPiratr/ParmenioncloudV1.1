"""Hostile and edge-case tests for the cameras module: users and agents poking
at each other's cameras, bad input, and limits."""

import uuid

import pytest
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from api.modules.auth import service as auth_service
from api.modules.cameras import service
from api.modules.cameras.models import Camera
from tests.test_cameras import (
    CAMERA,
    DEVICE,
    add_camera,
    agent_config,
    discover,
    discovered_camera,
    report_status,
)
from tests.test_edge_agent import bearer, enrolled_agent, make_user

# ---------- other people's cameras ----------


async def test_users_cannot_see_or_touch_each_others_cameras(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    alice = await make_user(db_session, "alice@example.com")
    bob = await make_user(db_session, "bob@example.com")
    agent_id, token = await enrolled_agent(client, alice)
    camera = await add_camera(client, alice, agent_id)
    found = await discovered_camera(client, alice, token)

    url = f"/cameras/{camera['id']}"
    assert (await client.get("/cameras", headers=bob)).json() == []
    assert (await client.get(url, headers=bob)).status_code == 404
    assert (await client.patch(url, json={"name": "x"}, headers=bob)).status_code == 404
    assert (await client.delete(url, headers=bob)).status_code == 404
    adopt = await client.post(
        f"/cameras/{found['id']}/adopt", json={"name": "mine"}, headers=bob
    )
    ignore = await client.post(f"/cameras/{found['id']}/ignore", headers=bob)
    onto_alices_agent = await client.post(
        "/cameras", json={**CAMERA, "agent_id": agent_id}, headers=bob
    )
    assert adopt.status_code == ignore.status_code == 404
    assert onto_alices_agent.status_code == 404
    # Nothing changed for Alice.
    assert (await client.get(url, headers=alice)).json()["name"] == "Front Gate"


async def test_agent_cannot_report_on_another_agents_cameras(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    alice = await make_user(db_session, "alice@example.com")
    bob = await make_user(db_session, "bob@example.com")
    alice_agent, _ = await enrolled_agent(client, alice)
    _, bob_token = await enrolled_agent(client, bob)
    camera = await add_camera(client, alice, alice_agent)

    response = await report_status(
        client,
        bob_token,
        {"camera_id": camera["id"], "state": "streaming"},
        {"camera_id": str(uuid.uuid4()), "state": "streaming"},
    )

    assert response.json() == {"updated": 0}
    after = (await client.get(f"/cameras/{camera['id']}", headers=alice)).json()
    assert after["stream_state"] == "unknown"
    assert (await agent_config(client, bob_token))["cameras"] == []


async def test_same_device_on_two_agents_is_two_cameras(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    alice = await make_user(db_session, "alice@example.com")
    bob = await make_user(db_session, "bob@example.com")
    _, alice_token = await enrolled_agent(client, alice)
    _, bob_token = await enrolled_agent(client, bob)

    assert (await discover(client, alice_token, DEVICE)).json()["new"] == 1
    assert (await discover(client, bob_token, DEVICE)).json()["new"] == 1
    assert len((await client.get("/cameras", headers=alice)).json()) == 1
    assert len((await client.get("/cameras", headers=bob)).json()) == 1


async def test_agent_endpoints_reject_user_token_revoked_agent_and_no_token(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    user = await make_user(db_session)
    agent_id, token = await enrolled_agent(client, user)
    await client.post(f"/agents/{agent_id}/revoke", headers=user)

    for headers in (user, bearer(token), {}):
        assert (await client.get("/agent/config", headers=headers)).status_code == 401
        response = await client.post(
            "/agent/cameras/discovered", json={"devices": [DEVICE]}, headers=headers
        )
        assert response.status_code == 401


async def test_disabled_owner_disables_agent_camera_endpoints(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    user = await make_user(db_session)
    _, token = await enrolled_agent(client, user)
    owner = await auth_service.get_user_from_token(
        db_session, user["Authorization"].split()[1]
    )
    owner.is_active = False
    await db_session.commit()

    assert (await client.get("/agent/config", headers=bearer(token))).status_code == 401


async def test_cannot_adopt_on_revoked_agent(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    user = await make_user(db_session)
    agent_id, token = await enrolled_agent(client, user)
    camera = await discovered_camera(client, user, token)
    await client.post(f"/agents/{agent_id}/revoke", headers=user)

    response = await client.post(
        f"/cameras/{camera['id']}/adopt", json={"name": "x"}, headers=user
    )

    assert response.status_code == 409


# ---------- hostile input ----------


@pytest.mark.parametrize(
    "url",
    [
        "http://192.168.1.20/stream",
        "rtsp://admin:pw@192.168.1.20/stream",
        "rtsp://user@192.168.1.20/stream",
        "rtsp://",
        "rtsp:///no-host",
        "rtsp://192.168.1.20:99999/stream",
        "rtsp://192.168.1.20:port/stream",
        "rtsp://192.168.1 .20/stream",
        "rtsp://192.168.1.20/str\x00eam",
        "rtsp://192.168.1.20/" + "a" * 500,
        "javascript:alert(1)",
        "",
    ],
)
async def test_bad_rtsp_urls_are_rejected(
    client: AsyncClient, db_session: AsyncSession, url: str
) -> None:
    user = await make_user(db_session)
    agent_id, _ = await enrolled_agent(client, user)

    response = await client.post(
        "/cameras", json={**CAMERA, "agent_id": agent_id, "rtsp_url": url}, headers=user
    )

    assert response.status_code == 422


async def test_unusual_but_valid_rtsp_urls_are_accepted(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    user = await make_user(db_session)
    agent_id, _ = await enrolled_agent(client, user)

    for url in (
        "RTSPS://cam.local:322/live?channel=1&subtype=0",
        "rtsp://[fe80::1]:554/h264",
        "  rtsp://10.0.0.2/stream  ",
    ):
        body = await add_camera(client, user, agent_id, rtsp_url=url)
        assert body["rtsp_url"] == url.strip()


@pytest.mark.parametrize("field", ["name", "username", "password"])
async def test_control_characters_are_rejected(
    client: AsyncClient, db_session: AsyncSession, field: str
) -> None:
    user = await make_user(db_session)
    agent_id, _ = await enrolled_agent(client, user)

    for bad in ("bad\x00value", "bad\nvalue"):
        response = await client.post(
            "/cameras",
            json={**CAMERA, "agent_id": agent_id, field: bad},
            headers=user,
        )
        assert response.status_code == 422, (field, bad)


async def test_long_fields_are_rejected(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    user = await make_user(db_session)
    agent_id, _ = await enrolled_agent(client, user)

    for field, length in (("name", 101), ("username", 101), ("password", 129)):
        response = await client.post(
            "/cameras",
            json={**CAMERA, "agent_id": agent_id, field: "x" * length},
            headers=user,
        )
        assert response.status_code == 422, field


async def test_unicode_password_round_trips_to_agent(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    user = await make_user(db_session)
    agent_id, token = await enrolled_agent(client, user)
    password = "pä ss wörd 🔑" + "x" * 115  # 128 characters, the maximum

    await add_camera(client, user, agent_id, name="Kamera Ü", password=password)

    [camera] = (await agent_config(client, token))["cameras"]
    assert camera["password"] == password


async def test_patch_rejects_null_for_required_fields(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    user = await make_user(db_session)
    agent_id, _ = await enrolled_agent(client, user)
    camera = await add_camera(client, user, agent_id)

    for field in ("name", "rtsp_url", "enabled", "record_enabled"):
        response = await client.patch(
            f"/cameras/{camera['id']}", json={field: None}, headers=user
        )
        assert response.status_code == 422, field


@pytest.mark.parametrize(
    "device",
    [
        {**DEVICE, "device_key": ""},
        {**DEVICE, "device_key": "k\x00"},
        {**DEVICE, "device_key": "k" * 129},
        {**DEVICE, "ip_address": "999.1.1.1"},
        {**DEVICE, "ip_address": "not-an-ip"},
        {**DEVICE, "vendor": "v" * 65},
        {**DEVICE, "streams": ["http://not-rtsp/"]},
        {**DEVICE, "streams": ["rtsp://a:b@host/"]},
        {**DEVICE, "streams": [f"rtsp://host/{i}" for i in range(9)]},
        {"ip_address": "192.168.1.20"},
    ],
)
async def test_bad_discovery_reports_are_rejected(
    client: AsyncClient, db_session: AsyncSession, device: dict
) -> None:
    user = await make_user(db_session)
    _, token = await enrolled_agent(client, user)

    assert (await discover(client, token, device)).status_code == 422


async def test_bad_status_reports_are_rejected(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    user = await make_user(db_session)
    _, token = await enrolled_agent(client, user)
    camera_id = str(uuid.uuid4())

    for report in (
        {"camera_id": camera_id, "state": "unknown"},
        {"camera_id": camera_id, "state": "streaming", "error": "x" * 501},
        {"camera_id": camera_id, "state": "error", "error": "a\x00b"},
        {"camera_id": "not-a-uuid", "state": "streaming"},
    ):
        assert (await report_status(client, token, report)).status_code == 422
    too_many = [{"camera_id": camera_id, "state": "streaming"}] * 65
    assert (await report_status(client, token, *too_many)).status_code == 422


async def test_error_text_is_dropped_when_streaming(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    user = await make_user(db_session)
    agent_id, token = await enrolled_agent(client, user)
    camera = await add_camera(client, user, agent_id)

    await report_status(
        client, token, {"camera_id": camera["id"], "state": "streaming", "error": "x"}
    )

    body = (await client.get(f"/cameras/{camera['id']}", headers=user)).json()
    assert body["stream_error"] is None


async def test_status_for_disabled_camera_is_ignored(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    user = await make_user(db_session)
    agent_id, token = await enrolled_agent(client, user)
    camera = await add_camera(client, user, agent_id)
    await client.patch(
        f"/cameras/{camera['id']}", json={"enabled": False}, headers=user
    )

    response = await report_status(
        client, token, {"camera_id": camera["id"], "state": "streaming"}
    )

    assert response.json() == {"updated": 0}


# ---------- limits ----------


async def test_camera_limit_per_agent(
    client: AsyncClient, db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(service, "MAX_CAMERAS_PER_AGENT", 2)
    user = await make_user(db_session)
    agent_id, token = await enrolled_agent(client, user)
    first = await add_camera(client, user, agent_id)
    await add_camera(client, user, agent_id)
    # Disabled cameras still count: they can be enabled at any time.
    await client.patch(f"/cameras/{first['id']}", json={"enabled": False}, headers=user)
    found = await discovered_camera(client, user, token)

    manual = await client.post(
        "/cameras", json={**CAMERA, "agent_id": agent_id}, headers=user
    )
    adopt = await client.post(
        f"/cameras/{found['id']}/adopt", json={"name": "x"}, headers=user
    )

    assert manual.status_code == adopt.status_code == 409


async def test_discovery_stops_storing_new_devices_at_row_limit(
    client: AsyncClient, db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(service, "MAX_ROWS_PER_AGENT", 3)
    user = await make_user(db_session)
    _, token = await enrolled_agent(client, user)
    devices = [{**DEVICE, "device_key": f"dev-{i}"} for i in range(5)]

    first = await discover(client, token, *devices)
    # Known devices are still updated once the limit is reached.
    second = await discover(client, token, *devices)

    assert first.json() == {"new": 3, "updated": 0, "skipped": 2}
    assert second.json() == {"new": 0, "updated": 3, "skipped": 2}
    assert len((await client.get("/cameras", headers=user)).json()) == 3


async def test_discovery_report_size_limit(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    user = await make_user(db_session)
    _, token = await enrolled_agent(client, user)
    devices = [{**DEVICE, "device_key": f"dev-{i}"} for i in range(257)]

    assert (await discover(client, token, *devices)).status_code == 422


async def test_repeated_device_in_one_report_is_stored_once(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    user = await make_user(db_session)
    agent_id, token = await enrolled_agent(client, user)

    response = await discover(
        client, token, DEVICE, {**DEVICE, "ip_address": "192.168.1.99"}
    )

    assert response.json() == {"new": 1, "updated": 0, "skipped": 0}
    [camera] = await db_session.scalars(
        select(Camera).where(Camera.agent_id == uuid.UUID(agent_id))
    )
    assert camera.ip_address == "192.168.1.99"


async def test_malformed_ids_in_paths(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    user = await make_user(db_session)

    assert (await client.get("/cameras/not-a-uuid", headers=user)).status_code == 422
    assert (
        await client.get(f"/cameras/{uuid.uuid4()}", headers=user)
    ).status_code == 404
    assert (
        await client.get("/cameras?agent_id=not-a-uuid", headers=user)
    ).status_code == 422
