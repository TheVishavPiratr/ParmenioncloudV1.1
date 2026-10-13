"""End-to-end journeys across every module, as a customer and their edge box
would go through them: account, agent, cameras, media, gallery.

Each test drives the public HTTP API and the agent's real WebSocket, and checks
that the modules hand work to each other correctly.
"""

import hashlib
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from httpx import AsyncClient
from httpx_ws import WebSocketDisconnect
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from api.core import storage
from api.modules.auth import service as auth_service
from api.modules.auth.models import User
from api.modules.device_link import service as device_service
from api.modules.media_access import service as media_service
from api.modules.media_access.models import Media
from tests.test_auth import last_code
from tests.test_edge_agent import (
    ENROLL,
    HEARTBEAT,
    Headers,
    agent_socket,
    bearer,
    commands_of,
    receive,
)

SNAPSHOT = b"\xff\xd8\xff\xe0" + b"gate-snapshot" * 200 + b"\xff\xd9"
CLIP = bytes(range(256)) * 1500
FACE_JPEG = b"\xff\xd8\xff\xe0" + b"asha-face" * 300 + b"\xff\xd9"
VECTOR = [0.25] * 128
MODEL = "arcface-r100"
DEVICE = {
    "device_key": "aa:bb:cc:dd:ee:01",
    "ip_address": "192.168.1.64",
    "vendor": "Hikvision",
    "model": "DS-2CD2143",
    "streams": ["rtsp://192.168.1.64:554/Streaming/101"],
}


@pytest.fixture(autouse=True)
def media_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setattr(storage, "root", tmp_path)
    return tmp_path


# ---------- helpers ----------


def files_in(root: Path) -> list[Path]:
    return [p for p in root.rglob("*") if p.is_file()]


async def sign_up(client: AsyncClient, outbox: list, email: str) -> Headers:
    """The real account flow: sign up, verify by emailed code, log in."""
    password = "correct-horse-battery"
    signup = await client.post(
        "/auth/signup",
        json={"email": email, "password": password, "full_name": "Customer"},
    )
    assert signup.status_code == 201, signup.text
    verify = await client.post(
        "/auth/verify-email", json={"email": email, "code": last_code(outbox)}
    )
    assert verify.status_code == 200, verify.text
    login = await client.post(
        "/auth/login", json={"email": email, "password": password}
    )
    assert login.status_code == 200, login.text
    return bearer(login.json()["access_token"])


async def set_up_site(
    client: AsyncClient, user: Headers, name: str = "Home"
) -> tuple[str, str]:
    """Create an agent and enroll the box. Returns (agent_id, agent_token)."""
    created = await client.post("/agents", json={"name": name}, headers=user)
    assert created.status_code == 201, created.text
    enrolled = await client.post(
        "/agent/enroll",
        json={**ENROLL, "enrollment_token": created.json()["enrollment_token"]},
    )
    assert enrolled.status_code == 200, enrolled.text
    return enrolled.json()["agent_id"], enrolled.json()["agent_token"]


async def ack(ws, command: dict) -> None:
    await ws.send_json({"type": "ack", "id": command["id"], "ok": True})
    assert await receive(ws) == {"type": "ack_received", "id": command["id"]}


async def upload(
    client: AsyncClient,
    token: str,
    camera_id: str,
    content: bytes,
    kind: str = "snapshot",
    content_type: str = "image/jpeg",
    **extra,
) -> str:
    registered = await client.post(
        "/agent/media",
        json={
            "camera_id": camera_id,
            "kind": kind,
            "content_type": content_type,
            "size_bytes": len(content),
            "sha256": hashlib.sha256(content).hexdigest(),
            "captured_at": datetime.now(UTC).isoformat(),
            **extra,
        },
        headers=bearer(token),
    )
    assert registered.status_code == 201, registered.text
    put = await client.put(
        registered.json()["upload_url"], content=content, headers=bearer(token)
    )
    assert put.status_code == 200, put.text
    return registered.json()["media_id"]


async def add_camera(
    client: AsyncClient, user: Headers, agent_id: str, **extra
) -> dict:
    response = await client.post(
        "/cameras",
        json={
            "agent_id": agent_id,
            "name": "Front Gate",
            "rtsp_url": "rtsp://192.168.1.64/stream",
            "username": "admin",
            "password": "cam-pass",
            **extra,
        },
        headers=user,
    )
    assert response.status_code == 201, response.text
    return response.json()


async def view(client: AsyncClient, user: Headers, path: str):
    """Ask for a signed link (path ends in /link) and open it without login."""
    link = await client.post(path, headers=user)
    assert link.status_code == 200, link.text
    return await client.get(link.json()["url"])


# ---------- journeys ----------


async def test_new_customer_goes_from_signup_to_watching_footage(
    client: AsyncClient, outbox: list, db_session: AsyncSession
) -> None:
    """Sign up, connect a box, discover and adopt a camera, see it go live,
    receive footage, and watch it, with the box on its WebSocket throughout."""
    user = await sign_up(client, outbox, "new.customer@example.com")
    agent_id, token = await set_up_site(client, user)

    async with agent_socket(token) as ws:
        hello = await receive(ws)
        assert hello == {"type": "hello", "agent_id": agent_id, "config_version": 1}

        beat = await client.post(
            "/agent/heartbeat", json=HEARTBEAT, headers=bearer(token)
        )
        assert beat.json()["config_version"] == 1
        agent = (await client.get(f"/agents/{agent_id}", headers=user)).json()
        assert agent["online"] is True and agent["status"] == "active"

        # The owner taps "scan": the command reaches the box at once.
        await client.post(f"/agents/{agent_id}/rescan", headers=user)
        scan = await receive(ws)
        assert scan["command"] == "rescan_cameras"
        found = await client.post(
            "/agent/cameras/discovered",
            json={"devices": [DEVICE]},
            headers=bearer(token),
        )
        assert found.json() == {"new": 1, "updated": 0, "skipped": 0}
        await ack(ws, scan)

        [discovered] = (
            await client.get("/cameras?state=discovered", headers=user)
        ).json()
        assert discovered["vendor"] == "Hikvision" and discovered["name"] is None

        # Adopting it pushes a config change; the box fetches its camera list.
        adopted = await client.post(
            f"/cameras/{discovered['id']}/adopt",
            json={"name": "Front Gate", "username": "admin", "password": "cam-pass"},
            headers=user,
        )
        assert adopted.status_code == 200
        change = await receive(ws)
        assert change["command"] == "config_changed"
        assert change["payload"] == {"config_version": 2}
        config = (await client.get("/agent/config", headers=bearer(token))).json()
        assert config["config_version"] == 2
        [camera] = config["cameras"]
        assert camera["rtsp_url"] == DEVICE["streams"][0]
        assert camera["password"] == "cam-pass"
        await ack(ws, change)

        # The box reports the stream running: the owner sees the camera online.
        await client.post(
            "/agent/cameras/status",
            json={"cameras": [{"camera_id": camera["id"], "state": "streaming"}]},
            headers=bearer(token),
        )
        live = (await client.get(f"/cameras/{camera['id']}", headers=user)).json()
        assert live["online"] is True

        # Something happens at the gate: the box uploads a snapshot and a clip.
        snapshot_id = await upload(client, token, camera["id"], SNAPSHOT)
        clip_id = await upload(
            client,
            token,
            camera["id"],
            CLIP,
            kind="clip",
            content_type="video/mp4",
            duration_s=8.0,
        )

    footage = (await client.get("/media", headers=user)).json()
    assert {m["id"] for m in footage} == {snapshot_id, clip_id}
    picture = await view(client, user, f"/media/{snapshot_id}/link")
    assert picture.content == SNAPSHOT
    link = await client.post(f"/media/{clip_id}/link", headers=user)
    seek = await client.get(link.json()["url"], headers={"Range": "bytes=100-199"})
    assert seek.status_code == 206 and seek.content == CLIP[100:200]

    agent_after = (await client.get(f"/agents/{agent_id}", headers=user)).json()
    statuses = [c.status for c in await commands_of(db_session, agent_id)]
    assert agent_after["config_version"] == 2
    assert statuses == ["acked", "acked"]


async def test_gallery_reaches_every_site_and_faces_are_computed_once(
    client: AsyncClient, outbox: list, media_root: Path
) -> None:
    """Two boxes of one owner, both connected. A new face goes to both; the
    first box computes the vector and the second one just receives it."""
    user = await sign_up(client, outbox, "two.sites@example.com")
    home_id, home = await set_up_site(client, user, "Home")
    shop_id, shop = await set_up_site(client, user, "Shop")

    # Tests share one database session across requests, so the sockets connect
    # one after the other; in production each connection has its own session.
    async with agent_socket(home) as home_ws:
        await receive(home_ws)
        async with agent_socket(shop) as shop_ws:
            await receive(shop_ws)

            person = (
                await client.post(
                    "/gallery/people",
                    json={
                        "name": "Asha",
                        "category": "family",
                        "consent_confirmed": True,
                    },
                    headers=user,
                )
            ).json()
            photo = (
                await client.post(
                    f"/gallery/people/{person['id']}/faces",
                    content=FACE_JPEG,
                    headers=user,
                )
            ).json()
            for ws in (home_ws, shop_ws):
                change = await receive(ws)
                assert change["command"] == "config_changed"
                await ack(ws, change)

            # Home computes the face vector from the photo.
            gallery = (
                await client.get(f"/agent/gallery?model={MODEL}", headers=bearer(home))
            ).json()
            face = gallery["people"][0]["faces"][0]
            image = await client.get(face["image_url"], headers=bearer(home))
            assert image.content == FACE_JPEG
            stored = await client.post(
                "/agent/gallery/embeddings",
                json={
                    "model": MODEL,
                    "results": [{"face_id": photo["id"], "vector": VECTOR}],
                },
                headers=bearer(home),
            )
            assert stored.json()["stored"] == 1

            # Only Shop is told; it gets the vector without computing anything.
            change = await receive(shop_ws)
            assert change["command"] == "config_changed"
            await ack(shop_ws, change)
            shop_gallery = (
                await client.get(f"/agent/gallery?model={MODEL}", headers=bearer(shop))
            ).json()
            assert shop_gallery["people"][0]["faces"] == [
                {"id": photo["id"], "embedding": VECTOR, "image_url": None}
            ]

            # Marking her as blocked reaches both boxes.
            await client.patch(
                f"/gallery/people/{person['id']}",
                json={"category": "blocked"},
                headers=user,
            )
            for ws, token in ((home_ws, home), (shop_ws, shop)):
                await ack(ws, await receive(ws))
                synced = (
                    await client.get(
                        f"/agent/gallery?model={MODEL}", headers=bearer(token)
                    )
                ).json()
                assert synced["people"][0]["category"] == "blocked"

            # Deleting her removes the photo everywhere.
            await client.delete(f"/gallery/people/{person['id']}", headers=user)
            for ws, token in ((home_ws, home), (shop_ws, shop)):
                await ack(ws, await receive(ws))
                synced = (
                    await client.get(
                        f"/agent/gallery?model={MODEL}", headers=bearer(token)
                    )
                ).json()
                assert synced["people"] == []

    assert files_in(media_root) == []
    person_page = await client.get(f"/gallery/people/{person['id']}", headers=user)
    assert person_page.status_code == 404
    assert home_id != shop_id


async def test_offline_box_catches_up_when_it_reconnects(
    client: AsyncClient, outbox: list, db_session: AsyncSession
) -> None:
    """Changes made while the box is offline are not lost, and it gets only
    the latest config change, not a pile of stale ones."""
    user = await sign_up(client, outbox, "offline@example.com")
    agent_id, token = await set_up_site(client, user)

    first = await add_camera(client, user, agent_id)
    await add_camera(
        client, user, agent_id, name="Back Door", rtsp_url="rtsp://10.0.0.2/s"
    )
    await client.patch(f"/cameras/{first['id']}", json={"enabled": False}, headers=user)

    # The heartbeat alone already tells the box it is behind.
    beat = await client.post("/agent/heartbeat", json=HEARTBEAT, headers=bearer(token))
    assert beat.json()["config_version"] == 4

    async with agent_socket(token) as ws:
        assert (await receive(ws))["config_version"] == 4
        backlog = await receive(ws)
        assert backlog["command"] == "config_changed"
        assert backlog["payload"] == {"config_version": 4}
        await ack(ws, backlog)

    config = (await client.get("/agent/config", headers=bearer(token))).json()
    assert [c["rtsp_url"] for c in config["cameras"]] == ["rtsp://10.0.0.2/s"]
    statuses = [c.status for c in await commands_of(db_session, agent_id)]
    assert statuses == ["expired", "expired", "acked"]


async def test_stolen_box_is_revoked_and_replaced(
    client: AsyncClient, outbox: list, db_session: AsyncSession
) -> None:
    """Revoking cuts the box off from every module at once; a replacement box
    picks up the same cameras and gallery; old footage stays viewable."""
    user = await sign_up(client, outbox, "stolen@example.com")
    agent_id, old_token = await set_up_site(client, user)
    camera = await add_camera(client, user, agent_id)
    media_id = await upload(client, old_token, camera["id"], SNAPSHOT)
    await client.post(
        "/agent/cameras/status",
        json={"cameras": [{"camera_id": camera["id"], "state": "streaming"}]},
        headers=bearer(old_token),
    )

    async with agent_socket(old_token) as ws:
        await receive(ws)  # hello
        queued = await receive(ws)  # the camera added while it was offline
        assert queued["command"] == "config_changed"
        await client.post(f"/agents/{agent_id}/revoke", headers=user)
        with pytest.raises(WebSocketDisconnect) as closed:
            await ws.receive_json(timeout=2)
        assert closed.value.code == 4401

    old = bearer(old_token)
    for method, path, kwargs in (
        ("GET", "/agent/config", {}),
        ("GET", f"/agent/gallery?model={MODEL}", {}),
        ("POST", "/agent/heartbeat", {"json": HEARTBEAT}),
        ("POST", "/agent/cameras/discovered", {"json": {"devices": [DEVICE]}}),
        ("POST", "/agent/cameras/status", {"json": {"cameras": []}}),
        ("PUT", f"/agent/media/{media_id}/content", {"content": SNAPSHOT}),
    ):
        response = await client.request(method, path, headers=old, **kwargs)
        assert response.status_code == 401, path
    assert (await client.get(f"/cameras/{camera['id']}", headers=user)).json()[
        "online"
    ] is False
    assert (await view(client, user, f"/media/{media_id}/link")).content == SNAPSHOT
    # No new cameras on a revoked box.
    blocked = await client.post(
        "/cameras",
        json={"agent_id": agent_id, "name": "X", "rtsp_url": "rtsp://10.0.0.9/s"},
        headers=user,
    )
    assert blocked.status_code == 409

    # A replacement box enrolls with a fresh token and gets the same cameras.
    fresh = await client.post(f"/agents/{agent_id}/enrollment-token", headers=user)
    enrolled = await client.post(
        "/agent/enroll",
        json={**ENROLL, "enrollment_token": fresh.json()["enrollment_token"]},
    )
    new_token = enrolled.json()["agent_token"]
    config = (await client.get("/agent/config", headers=bearer(new_token))).json()
    assert [c["id"] for c in config["cameras"]] == [camera["id"]]
    assert (await client.get("/agent/config", headers=old)).status_code == 401


async def test_removing_a_site_keeps_its_footage_until_retention(
    client: AsyncClient, outbox: list, db_session: AsyncSession, media_root: Path
) -> None:
    user = await sign_up(client, outbox, "remove.site@example.com")
    agent_id, token = await set_up_site(client, user)
    camera = await add_camera(client, user, agent_id)
    media_id = await upload(client, token, camera["id"], SNAPSHOT)

    deleted = await client.delete(f"/agents/{agent_id}", headers=user)

    assert deleted.status_code == 204
    assert (await client.get("/cameras", headers=user)).json() == []
    [kept] = (await client.get("/media", headers=user)).json()
    assert kept["id"] == media_id
    assert kept["camera_id"] is None and kept["agent_id"] is None
    assert (await view(client, user, f"/media/{media_id}/link")).content == SNAPSHOT

    # Once older than the retention period, the cloud deletes it by itself.
    await db_session.execute(
        update(Media)
        .where(Media.id == uuid.UUID(media_id))
        .values(captured_at=datetime.now(UTC) - timedelta(days=31))
    )
    await db_session.commit()
    assert await media_service.purge_expired(db_session) == 1
    assert (await client.get("/media", headers=user)).json() == []
    assert files_in(media_root) == []


async def test_disabled_account_cuts_off_user_and_boxes(
    client: AsyncClient, outbox: list, db_session: AsyncSession
) -> None:
    user = await sign_up(client, outbox, "disabled@example.com")
    _, token = await set_up_site(client, user)

    account = await db_session.scalar(
        select(User).where(User.email == "disabled@example.com")
    )
    account.is_active = False
    await db_session.commit()

    assert (await client.get("/cameras", headers=user)).status_code == 401
    assert (await client.get("/media", headers=user)).status_code == 401
    assert (await client.get("/gallery/people", headers=user)).status_code == 401
    assert (await client.get("/agent/config", headers=bearer(token))).status_code == 401
    login = await client.post(
        "/auth/login",
        json={"email": "disabled@example.com", "password": "correct-horse-battery"},
    )
    assert login.status_code == 403


async def test_two_customers_are_fully_isolated(
    client: AsyncClient, outbox: list
) -> None:
    """Every kind of record of one customer, tried by the other customer and
    by the other customer's box."""
    alice = await sign_up(client, outbox, "alice.e2e@example.com")
    bob = await sign_up(client, outbox, "bob.e2e@example.com")
    alice_agent, alice_token = await set_up_site(client, alice)
    _, bob_token = await set_up_site(client, bob)

    camera = await add_camera(client, alice, alice_agent)
    media_id = await upload(client, alice_token, camera["id"], SNAPSHOT)
    person = (
        await client.post(
            "/gallery/people",
            json={"name": "Asha", "category": "family", "consent_confirmed": True},
            headers=alice,
        )
    ).json()
    photo = (
        await client.post(
            f"/gallery/people/{person['id']}/faces", content=FACE_JPEG, headers=alice
        )
    ).json()

    # Bob, logged in, sees nothing of Alice's and can reach none of it.
    bob_agents = (await client.get("/agents", headers=bob)).json()
    assert alice_agent not in [a["id"] for a in bob_agents]
    for path in ("/cameras", "/media", "/gallery/people"):
        assert (await client.get(path, headers=bob)).json() == [], path
    for method, path in (
        ("GET", f"/agents/{alice_agent}"),
        ("GET", f"/cameras/{camera['id']}"),
        ("GET", f"/media/{media_id}"),
        ("POST", f"/media/{media_id}/link"),
        ("GET", f"/gallery/people/{person['id']}"),
        ("POST", f"/gallery/faces/{photo['id']}/link"),
        ("DELETE", f"/gallery/faces/{photo['id']}"),
        ("DELETE", f"/media/{media_id}"),
        ("DELETE", f"/cameras/{camera['id']}"),
        ("DELETE", f"/agents/{alice_agent}"),
    ):
        response = await client.request(method, path, headers=bob)
        assert response.status_code == 404, (method, path)

    # Bob's box gets none of Alice's config, gallery or uploads.
    bob_box = bearer(bob_token)
    assert (await client.get("/agent/config", headers=bob_box)).json()["cameras"] == []
    gallery = await client.get(f"/agent/gallery?model={MODEL}", headers=bob_box)
    assert gallery.json()["people"] == []
    image = await client.get(
        f"/agent/gallery/faces/{photo['id']}/image", headers=bob_box
    )
    assert image.status_code == 404
    register = await client.post(
        "/agent/media",
        json={
            "camera_id": camera["id"],
            "kind": "snapshot",
            "content_type": "image/jpeg",
            "size_bytes": 10,
            "sha256": "a" * 64,
            "captured_at": datetime.now(UTC).isoformat(),
        },
        headers=bob_box,
    )
    assert register.status_code == 404

    # And Alice still has everything.
    assert len((await client.get("/media", headers=alice)).json()) == 1
    assert (
        await view(client, alice, f"/gallery/faces/{photo['id']}/link")
    ).content == (FACE_JPEG)


async def test_phone_stays_logged_in_until_stolen_and_owner_resets_password(
    client: AsyncClient, outbox: list, db_session: AsyncSession
) -> None:
    """The phone app logs in once and keeps refreshing; it registers for push
    alerts. The phone is stolen: the owner resets the password on the web and
    the phone loses access to everything, alerts included."""
    email = "phone.journey@example.com"
    await sign_up(client, outbox, email)
    login = await client.post(
        "/devices/login",
        json={
            "email": email,
            "password": "correct-horse-battery",
            "device": {"name": "Pixel 8", "platform": "android"},
        },
    )
    pair = login.json()
    user_id = auth_service.get_token_claims(pair["access_token"]).user_id

    # A day of use: the app refreshes several times and uses the API.
    for _ in range(3):
        pair = (
            await client.post(
                "/devices/refresh", json={"refresh_token": pair["refresh_token"]}
            )
        ).json()
        phone = bearer(pair["access_token"])
        assert (await client.get("/cameras", headers=phone)).status_code == 200
    await client.put(
        f"/devices/{pair['device_id']}/push-token",
        json={"provider": "fcm", "token": "fcm-token-of-the-phone"},
        headers=phone,
    )
    assert len(await device_service.list_push_targets(db_session, user_id)) == 1

    # Stolen. The owner resets the password from a computer.
    await client.post("/auth/forgot-password", json={"email": email})
    await client.post(
        "/auth/reset-password",
        json={
            "email": email,
            "code": last_code(outbox),
            "new_password": "brand-new-pw",
        },
    )

    assert (await client.get("/cameras", headers=phone)).status_code == 401
    thief = await client.post(
        "/devices/refresh", json={"refresh_token": pair["refresh_token"]}
    )
    assert thief.status_code == 401
    assert await device_service.list_push_targets(db_session, user_id) == []
    web = await client.post(
        "/auth/login", json={"email": email, "password": "brand-new-pw"}
    )
    assert (
        await client.get("/devices", headers=bearer(web.json()["access_token"]))
    ).json() == []
