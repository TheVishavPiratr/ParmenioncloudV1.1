"""Tests for the account module: deleting an account removes every row and
file of the user in every module, cuts off all their tokens and boxes, and
leaves other customers untouched."""

import uuid
from datetime import UTC, datetime
from pathlib import Path

import pytest
from httpx import AsyncClient
from httpx_ws import WebSocketDisconnect
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from api.core import storage
from api.modules.audit.models import AuditEntry
from api.modules.auth.models import OtpCode, User
from api.modules.cameras.models import Camera
from api.modules.device_link.models import LinkedDevice
from api.modules.edge_agent.models import AgentCommand, EdgeAgent
from api.modules.events.models import Event
from api.modules.gallery.models import Face, FaceEmbedding, Person
from api.modules.media_access.models import Media
from api.modules.notifications.models import Notification, NotificationPreference
from tests.test_edge_agent import Headers, agent_socket, bearer, receive
from tests.test_end_to_end import (
    FACE_JPEG,
    MODEL,
    SNAPSHOT,
    VECTOR,
    add_camera,
    set_up_site,
    sign_up,
    upload,
)

PASSWORD = "correct-horse-battery"  # what sign_up uses


@pytest.fixture(autouse=True)
def media_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setattr(storage, "root", tmp_path)
    return tmp_path


def files_in(root: Path) -> set[Path]:
    return {p for p in root.rglob("*") if p.is_file()}


async def customer(client: AsyncClient, outbox: list, email: str) -> dict:
    """A customer with something in every module: a phone, a box with a
    camera, footage, a gallery person with a computed face, an event,
    notifications, a changed setting and audit entries."""
    user = await sign_up(client, outbox, email)
    phone = (
        await client.post(
            "/devices/login",
            json={
                "email": email,
                "password": PASSWORD,
                "device": {"name": "Phone", "platform": "android"},
            },
        )
    ).json()
    await client.put(
        f"/devices/{phone['device_id']}/push-token",
        json={"provider": "fcm", "token": f"push-{uuid.uuid4().hex}"},
        headers=user,
    )
    await client.patch(
        "/notifications/preferences/detection", json={"email": True}, headers=user
    )
    agent_id, token = await set_up_site(client, user)
    camera = await add_camera(client, user, agent_id)
    snapshot = await upload(client, token, camera["id"], SNAPSHOT)
    person = (
        await client.post(
            "/gallery/people",
            json={"name": "Asha", "category": "family", "consent_confirmed": True},
            headers=user,
        )
    ).json()
    face = (
        await client.post(
            f"/gallery/people/{person['id']}/faces", content=FACE_JPEG, headers=user
        )
    ).json()
    stored = await client.post(
        "/agent/gallery/embeddings",
        json={"model": MODEL, "results": [{"face_id": face["id"], "vector": VECTOR}]},
        headers=bearer(token),
    )
    assert stored.json()["stored"] == 1, stored.text
    reported = await client.post(
        "/agent/events",
        json={
            "events": [
                {
                    "client_event_id": "e1",
                    "camera_id": camera["id"],
                    "type": "face_recognized",
                    "person_id": person["id"],
                    "snapshot_media_id": snapshot,
                    "occurred_at": datetime.now(UTC).isoformat(),
                }
            ]
        },
        headers=bearer(token),
    )
    assert reported.json()["results"][0]["status"] == "created", reported.text
    me = (await client.get("/auth/me", headers=user)).json()
    return {
        "headers": user,
        "id": uuid.UUID(me["id"]),
        "email": email,
        "agent_id": uuid.UUID(agent_id),
        "agent_token": token,
        "refresh_token": phone["refresh_token"],
        "phone_token": phone["access_token"],
    }


async def rows_of(db: AsyncSession, who: dict) -> dict[str, int]:
    """How many rows the customer has in every table."""
    user_id, agent_id = who["id"], who["agent_id"]

    async def count(model, *where) -> int:
        return await db.scalar(select(func.count()).select_from(model).where(*where))

    return {
        "users": await count(User, User.id == user_id),
        "otp_codes": await count(OtpCode, OtpCode.user_id == user_id),
        "linked_devices": await count(LinkedDevice, LinkedDevice.user_id == user_id),
        "edge_agents": await count(EdgeAgent, EdgeAgent.owner_id == user_id),
        "agent_commands": await count(AgentCommand, AgentCommand.agent_id == agent_id),
        "cameras": await count(Camera, Camera.owner_id == user_id),
        "media": await count(Media, Media.owner_id == user_id),
        "gallery_people": await count(Person, Person.owner_id == user_id),
        "gallery_faces": await count(Face, Face.owner_id == user_id),
        "face_embeddings": await count(
            FaceEmbedding, FaceEmbedding.computed_by_agent_id == agent_id
        ),
        "events": await count(Event, Event.owner_id == user_id),
        "notifications": await count(Notification, Notification.user_id == user_id),
        "notification_preferences": await count(
            NotificationPreference, NotificationPreference.user_id == user_id
        ),
        "audit_log": await count(AuditEntry, AuditEntry.user_id == user_id),
    }


async def delete_account(client: AsyncClient, user: Headers, password: str = PASSWORD):
    return await client.post(
        "/account/delete", json={"password": password}, headers=user
    )


# ---------- deleting ----------


async def test_deleting_removes_everything_of_the_user_only(
    client: AsyncClient, outbox: list, db_session: AsyncSession, media_root: Path
) -> None:
    alice = await customer(client, outbox, "alice@example.com")
    bob_files_before = files_in(media_root)
    bob = await customer(client, outbox, "bob@example.com")
    bob_files = files_in(media_root) - bob_files_before
    bob_rows = await rows_of(db_session, bob)
    alice_rows = await rows_of(db_session, alice)
    # The setup really put something in every table.
    assert all(alice_rows.values()), alice_rows

    response = await delete_account(client, alice["headers"])

    assert response.status_code == 204, response.text
    assert set((await rows_of(db_session, alice)).values()) == {0}
    assert await rows_of(db_session, bob) == bob_rows
    assert files_in(media_root) == bob_files  # Alice's footage and photos are gone


async def test_everything_stops_working_at_once(
    client: AsyncClient, outbox: list
) -> None:
    alice = await customer(client, outbox, "alice@example.com")

    await delete_account(client, alice["headers"])

    assert (await client.get("/auth/me", headers=alice["headers"])).status_code == 401
    phone = bearer(alice["phone_token"])
    assert (await client.get("/events", headers=phone)).status_code == 401
    refreshed = await client.post(
        "/devices/refresh", json={"refresh_token": alice["refresh_token"]}
    )
    assert refreshed.status_code == 401
    box = bearer(alice["agent_token"])
    assert (await client.get("/agent/config", headers=box)).status_code == 401
    login = await client.post(
        "/auth/login", json={"email": alice["email"], "password": PASSWORD}
    )
    assert login.status_code == 401


async def test_connected_box_is_cut_off(client: AsyncClient, outbox: list) -> None:
    user = await sign_up(client, outbox, "alice@example.com")
    _, token = await set_up_site(client, user)

    async with agent_socket(token) as ws:
        assert (await receive(ws))["type"] == "hello"
        await delete_account(client, user)
        with pytest.raises(WebSocketDisconnect) as closed:
            await receive(ws)

    assert closed.value.code == 4401


async def test_only_an_anonymous_record_and_an_email_remain(
    client: AsyncClient, outbox: list, db_session: AsyncSession
) -> None:
    alice = await customer(client, outbox, "alice@example.com")

    await delete_account(client, alice["headers"])

    entry = await db_session.scalar(
        select(AuditEntry).where(AuditEntry.action == "account_deleted")
    )
    assert entry.user_id is None
    assert entry.target_type == "user"
    assert entry.target_id == alice["id"]
    assert entry.details == {"agents": 1, "media_files": 1, "face_photos": 1}
    assert "alice" not in str(entry.details)
    assert outbox[-1]["to"] == "alice@example.com"
    assert outbox[-1]["subject"] == "Your VisionPulse account was deleted"


async def test_email_can_sign_up_again(client: AsyncClient, outbox: list) -> None:
    alice = await customer(client, outbox, "alice@example.com")
    await delete_account(client, alice["headers"])

    again = await sign_up(client, outbox, "alice@example.com")

    assert (await client.get("/events", headers=again)).json() == []
    assert (await client.get("/agents", headers=again)).json() == []


# ---------- refusing ----------


async def test_wrong_password_deletes_nothing(
    client: AsyncClient, outbox: list, db_session: AsyncSession, media_root: Path
) -> None:
    alice = await customer(client, outbox, "alice@example.com")
    rows = await rows_of(db_session, alice)
    files = files_in(media_root)

    response = await delete_account(client, alice["headers"], "not-my-password")

    assert response.status_code == 403
    assert response.json() == {"detail": "Incorrect password"}
    after = await rows_of(db_session, alice)
    assert after == {**rows, "audit_log": rows["audit_log"] + 1}
    assert files_in(media_root) == files
    failed = await client.get(
        "/audit",
        params={"action": "account_deletion_failed"},
        headers=alice["headers"],
    )
    assert failed.json()[0]["details"] == {"reason": "wrong_password"}
    assert failed.json()[0]["success"] is False


async def test_needs_login_and_password(client: AsyncClient, outbox: list) -> None:
    user = await sign_up(client, outbox, "alice@example.com")

    assert (
        await client.post("/account/delete", json={"password": PASSWORD})
    ).status_code == 401
    assert (
        await client.post("/account/delete", json={}, headers=user)
    ).status_code == 422
    assert (
        await client.post("/account/delete", json={"password": ""}, headers=user)
    ).status_code == 422
    assert (await client.get("/auth/me", headers=user)).status_code == 200


async def test_agent_token_cannot_delete_an_account(
    client: AsyncClient, outbox: list
) -> None:
    user = await sign_up(client, outbox, "alice@example.com")
    _, token = await set_up_site(client, user)

    response = await delete_account(client, bearer(token))

    assert response.status_code == 401
    assert (await client.get("/auth/me", headers=user)).status_code == 200
