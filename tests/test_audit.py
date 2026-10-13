"""Tests for the audit module: what each module records, who can read it,
paging and filtering, retention, and that a failed audit write never breaks
the action being audited."""

import json
import uuid
from datetime import UTC, datetime, timedelta

import pytest
from httpx import AsyncClient
from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from api.modules.audit import context
from api.modules.audit import service as audit_service
from api.modules.audit.models import AuditEntry
from api.modules.auth.models import User
from api.modules.device_link import service as device_service
from api.modules.edge_agent.models import EdgeAgent
from tests.test_edge_agent import ENROLL, Headers, bearer
from tests.test_end_to_end import (
    SNAPSHOT,
    add_camera,
    media_root,  # noqa: F401  (autouse: keeps uploaded files in a temp folder)
    set_up_site,
    sign_up,
    upload,
)
from tests.test_gallery import JPEG, create_person

EMAIL = "audited@example.com"
PASSWORD = "correct-horse-battery"  # what sign_up uses
PHONE = {"name": "Pixel 8", "platform": "android"}


# ---------- helpers ----------


async def entries(client: AsyncClient, user: Headers, **params) -> list[dict]:
    response = await client.get("/audit", params=params, headers=user)
    assert response.status_code == 200, response.text
    return response.json()


async def actions(client: AsyncClient, user: Headers) -> list[str]:
    """Every recorded action, oldest first."""
    return [e["action"] for e in reversed(await entries(client, user, limit=100))]


async def latest(client: AsyncClient, user: Headers, action: str) -> dict:
    found = await entries(client, user, action=action, limit=1)
    assert found, f"no {action} entry"
    return found[0]


# ---------- auth ----------


async def test_account_flow_is_recorded_with_ip_and_user_agent(
    client: AsyncClient, outbox: list
) -> None:
    user = await sign_up(client, outbox, EMAIL)

    assert await actions(client, user) == [
        "signup",
        "email_verified",
        "login_succeeded",
    ]
    login = await latest(client, user, "login_succeeded")
    assert login["success"] is True
    assert login["actor"] == "user"
    assert login["ip"] == "127.0.0.1"
    assert login["user_agent"].startswith("python-httpx/")


async def test_failed_logins_are_recorded_with_a_reason(
    client: AsyncClient, outbox: list, db_session: AsyncSession
) -> None:
    user = await sign_up(client, outbox, EMAIL)

    await client.post("/auth/login", json={"email": EMAIL, "password": "wrong-one"})
    await client.post(
        "/devices/login",
        json={"email": EMAIL, "password": "wrong-two", "device": PHONE},
    )
    for email in ("nobody@example.com", " NoBody@Example.com", "other@example.com"):
        await client.post("/auth/login", json={"email": email, "password": "whatever"})

    failed = await entries(client, user, action="login_failed")
    assert len(failed) == 2
    for entry in failed:
        assert entry["success"] is False
        assert entry["actor"] == "anonymous"
        assert entry["details"] == {"reason": "wrong_password"}
    # An unknown email belongs to nobody, so no user can read it. It is kept
    # as a fingerprint only, the same for every spelling of one address.
    orphans = list(
        await db_session.scalars(
            select(AuditEntry)
            .where(AuditEntry.user_id.is_(None))
            .order_by(AuditEntry.created_at)
        )
    )
    assert {(e.action, e.details["reason"]) for e in orphans} == {
        ("login_failed", "unknown_email")
    }
    prints = [e.details["email_fingerprint"] for e in orphans]
    assert prints[0] == prints[1] != prints[2]
    assert "nobody" not in json.dumps([e.details for e in orphans]).lower()


async def test_unverified_login_is_recorded(
    client: AsyncClient, outbox: list, db_session: AsyncSession
) -> None:
    await client.post(
        "/auth/signup",
        json={"email": EMAIL, "password": PASSWORD, "full_name": "Pending"},
    )
    response = await client.post(
        "/auth/login", json={"email": EMAIL, "password": PASSWORD}
    )

    assert response.status_code == 403
    entry = await db_session.scalar(
        select(AuditEntry).where(AuditEntry.action == "login_failed")
    )
    assert entry.details == {"reason": "email_not_verified"}


async def test_password_reset_is_recorded(client: AsyncClient, outbox: list) -> None:
    await sign_up(client, outbox, EMAIL)
    await client.post("/auth/forgot-password", json={"email": EMAIL})
    code = outbox[-1]["body"].split("is: ")[1][:6]
    reset = await client.post(
        "/auth/reset-password",
        json={"email": EMAIL, "code": code, "new_password": "brand-new-password"},
    )
    assert reset.status_code == 200, reset.text
    login = await client.post(
        "/auth/login", json={"email": EMAIL, "password": "brand-new-password"}
    )
    user = bearer(login.json()["access_token"])

    requested = await latest(client, user, "password_reset_requested")
    assert requested["actor"] == "anonymous"
    assert (await latest(client, user, "password_reset"))["actor"] == "user"


# ---------- device_link ----------


async def test_device_actions_are_recorded(client: AsyncClient, outbox: list) -> None:
    web = await sign_up(client, outbox, EMAIL)
    login = {"email": EMAIL, "password": PASSWORD, "device": PHONE}
    first = (await client.post("/devices/login", json=login)).json()
    second = (await client.post("/devices/login", json=login)).json()

    await client.patch(
        f"/devices/{first['device_id']}",
        json={"name": "Old phone"},
        headers=web,
    )
    # Using a refresh token twice means it was copied: the device is dropped.
    await client.post(
        "/devices/refresh", json={"refresh_token": first["refresh_token"]}
    )
    await client.post(
        "/devices/refresh", json={"refresh_token": first["refresh_token"]}
    )
    await client.post("/devices/logout-all", headers=bearer(second["access_token"]))

    linked = await entries(client, web, action="device_linked")
    assert {e["target_id"] for e in linked} == {
        first["device_id"],
        second["device_id"],
    }
    assert linked[0]["details"] == {"name": "Pixel 8", "platform": "android"}
    updated = await latest(client, web, "device_updated")
    assert updated["details"] == {"fields": ["name"]}
    reused = await latest(client, web, "refresh_token_reused")
    assert reused["target_id"] == first["device_id"]
    assert reused["success"] is False
    assert (await latest(client, web, "devices_logged_out_all"))["details"] == {
        "count": 0
    }


async def test_device_logout_and_remove_are_recorded(
    client: AsyncClient, outbox: list
) -> None:
    web = await sign_up(client, outbox, EMAIL)
    login = {"email": EMAIL, "password": PASSWORD, "device": PHONE}
    phone = (await client.post("/devices/login", json=login)).json()
    tablet = (await client.post("/devices/login", json=login)).json()

    await client.post("/devices/logout", json={"refresh_token": phone["refresh_token"]})
    await client.delete(f"/devices/{tablet['device_id']}", headers=web)

    out = await latest(client, web, "device_logged_out")
    assert out["target_id"] == phone["device_id"]
    removed = await latest(client, web, "device_removed")
    assert removed["target_id"] == tablet["device_id"]


async def test_push_tokens_and_device_limit_are_recorded(
    client: AsyncClient, outbox: list, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(device_service, "MAX_DEVICES_PER_USER", 1)
    web = await sign_up(client, outbox, EMAIL)
    login = {"email": EMAIL, "password": PASSWORD, "device": PHONE}
    old = (await client.post("/devices/login", json=login)).json()
    new = (await client.post("/devices/login", json=login)).json()
    phone = bearer(new["access_token"])
    path = f"/devices/{new['device_id']}/push-token"

    set_token = await client.put(
        path, json={"provider": "fcm", "token": "secret-fcm-token"}, headers=phone
    )
    assert set_token.status_code == 200, set_token.text
    await client.delete(path, headers=phone)

    evicted = await latest(client, web, "device_evicted")
    assert evicted["target_id"] == old["device_id"]
    assert evicted["details"] == {"reason": "device_limit"}
    pushed = await latest(client, web, "push_token_set")
    assert pushed["target_id"] == new["device_id"]
    assert pushed["details"] == {"provider": "fcm"}
    cleared = await latest(client, web, "push_token_cleared")
    assert cleared["target_id"] == new["device_id"]
    assert "secret-fcm-token" not in json.dumps(await entries(client, web, limit=100))


# ---------- edge_agent and cameras ----------


async def test_agent_and_camera_lifecycle_is_recorded(
    client: AsyncClient, outbox: list
) -> None:
    user = await sign_up(client, outbox, EMAIL)
    agent_id, _ = await set_up_site(client, user)
    camera = await add_camera(client, user, agent_id)
    await client.patch(
        f"/cameras/{camera['id']}",
        json={"name": "Back Gate", "password": "new-cam-secret"},
        headers=user,
    )
    await client.delete(f"/cameras/{camera['id']}", headers=user)
    await client.patch(f"/agents/{agent_id}", json={"name": "Shop"}, headers=user)
    await client.post(f"/agents/{agent_id}/enrollment-token", headers=user)
    await client.post(f"/agents/{agent_id}/revoke", headers=user)
    await client.delete(f"/agents/{agent_id}", headers=user)

    assert (await actions(client, user))[3:] == [
        "agent_created",
        "agent_enrolled",
        "camera_added",
        "camera_updated",
        "camera_deleted",
        "agent_renamed",
        "agent_enrollment_token_issued",
        "agent_revoked",
        "agent_deleted",
    ]
    enrolled = await latest(client, user, "agent_enrolled")
    assert enrolled["actor"] == "agent"
    assert enrolled["agent_id"] == agent_id
    assert enrolled["details"]["hostname"] == "edge-box"
    updated = await latest(client, user, "camera_updated")
    assert updated["target_id"] == camera["id"]
    assert updated["details"] == {"fields": ["name", "password"]}
    # Never a secret: neither the camera password nor any token.
    everything = json.dumps(await entries(client, user, limit=100))
    assert "cam-pass" not in everything
    assert "new-cam-secret" not in everything
    assert "vpe_" not in everything and "vpa_" not in everything


async def test_failed_enrollments_are_recorded(
    client: AsyncClient, outbox: list, db_session: AsyncSession
) -> None:
    user = await sign_up(client, outbox, EMAIL)
    created = (await client.post("/agents", json={"name": "Home"}, headers=user)).json()
    await db_session.execute(
        update(EdgeAgent).values(enrollment_expires_at=datetime.now(UTC))
    )
    await db_session.commit()

    for token in (created["enrollment_token"], "vpe_made-up", "garbage"):
        response = await client.post(
            "/agent/enroll", json={**ENROLL, "enrollment_token": token}
        )
        assert response.status_code == 400, response.text

    expired = await latest(client, user, "agent_enrollment_failed")
    assert expired["target_id"] == created["agent"]["id"]
    assert expired["actor"] == "anonymous"
    assert expired["details"] == {"reason": "expired_or_used"}
    orphans = await db_session.scalars(
        select(AuditEntry).where(AuditEntry.user_id.is_(None))
    )
    assert sorted(e.details["reason"] for e in orphans) == [
        "malformed_token",
        "unknown_token",
    ]


async def test_owner_disabled_enrollment_is_recorded(
    client: AsyncClient, outbox: list, db_session: AsyncSession
) -> None:
    user = await sign_up(client, outbox, EMAIL)
    created = (await client.post("/agents", json={"name": "Home"}, headers=user)).json()
    await db_session.execute(update(User).values(is_active=False))
    await db_session.commit()

    response = await client.post(
        "/agent/enroll",
        json={**ENROLL, "enrollment_token": created["enrollment_token"]},
    )

    assert response.status_code == 400
    entry = await db_session.scalar(
        select(AuditEntry).where(AuditEntry.action == "agent_enrollment_failed")
    )
    assert entry.details == {"reason": "owner_disabled"}
    assert str(entry.target_id) == created["agent"]["id"]


async def test_discovered_camera_adopt_and_ignore_are_recorded(
    client: AsyncClient, outbox: list
) -> None:
    user = await sign_up(client, outbox, EMAIL)
    _, token = await set_up_site(client, user)
    found = await client.post(
        "/agent/cameras/discovered",
        json={
            "devices": [
                {"device_key": "aa:00:00:00:00:01", "streams": ["rtsp://10.0.0.5/1"]},
                {"device_key": "aa:00:00:00:00:02", "streams": ["rtsp://10.0.0.6/1"]},
            ]
        },
        headers=bearer(token),
    )
    assert found.status_code == 200, found.text
    cameras = (
        await client.get("/cameras", params={"state": "discovered"}, headers=user)
    ).json()
    first, second = cameras[0]["id"], cameras[1]["id"]

    adopted = await client.post(
        f"/cameras/{first}/adopt", json={"name": "Porch"}, headers=user
    )
    assert adopted.status_code == 200, adopted.text
    await client.post(f"/cameras/{second}/ignore", headers=user)

    assert (await latest(client, user, "camera_adopted"))["target_id"] == first
    assert (await latest(client, user, "camera_ignored"))["target_id"] == second


# ---------- media_access and gallery ----------


async def test_media_link_and_delete_are_recorded(
    client: AsyncClient, outbox: list
) -> None:
    user = await sign_up(client, outbox, EMAIL)
    agent_id, token = await set_up_site(client, user)
    camera = await add_camera(client, user, agent_id)
    media_id = await upload(client, token, camera["id"], SNAPSHOT)

    for _ in range(3):  # reloading the viewer asks for a fresh link each time
        link = await client.post(f"/media/{media_id}/link", headers=user)
        assert link.status_code == 200, link.text
    assert len(await entries(client, user, action="media_link_created")) == 1
    await client.delete(f"/media/{media_id}", headers=user)

    assert (await latest(client, user, "media_link_created"))["target_id"] == media_id
    deleted = await latest(client, user, "media_deleted")
    assert deleted["target_id"] == media_id
    assert deleted["details"] == {"kind": "snapshot", "camera_id": camera["id"]}


async def test_gallery_changes_are_recorded(client: AsyncClient, outbox: list) -> None:
    user = await sign_up(client, outbox, EMAIL)
    person = await create_person(client, user)
    await client.patch(
        f"/gallery/people/{person['id']}", json={"notes": "neighbour"}, headers=user
    )
    face = await client.post(
        f"/gallery/people/{person['id']}/faces", content=JPEG, headers=user
    )
    assert face.status_code == 201, face.text
    face_id = face.json()["id"]
    # The same photo again is not a new face.
    await client.post(
        f"/gallery/people/{person['id']}/faces", content=JPEG, headers=user
    )
    await client.post(f"/gallery/faces/{face_id}/link", headers=user)
    await client.post(f"/gallery/faces/{face_id}/link", headers=user)
    await client.delete(f"/gallery/faces/{face_id}", headers=user)
    await client.delete(f"/gallery/people/{person['id']}", headers=user)

    assert (await actions(client, user))[3:] == [
        "person_created",
        "person_updated",
        "face_added",
        "face_link_created",
        "face_deleted",
        "person_deleted",
    ]
    assert (await latest(client, user, "person_updated"))["details"] == {
        "fields": ["notes"]
    }
    added = await latest(client, user, "face_added")
    assert added["target_id"] == face_id
    assert added["details"] == {"person_id": person["id"]}


async def test_link_entries_repeat_once_the_window_has_passed(
    client: AsyncClient, outbox: list, db_session: AsyncSession
) -> None:
    user = await sign_up(client, outbox, EMAIL)
    person = await create_person(client, user)
    face = await client.post(
        f"/gallery/people/{person['id']}/faces", content=JPEG, headers=user
    )
    path = f"/gallery/faces/{face.json()['id']}/link"

    await client.post(path, headers=user)
    await db_session.execute(
        update(AuditEntry)
        .where(AuditEntry.action == "face_link_created")
        .values(created_at=datetime.now(UTC) - timedelta(minutes=11))
    )
    await db_session.commit()
    await client.post(path, headers=user)

    assert len(await entries(client, user, action="face_link_created")) == 2


# ---------- reading the log ----------


async def test_log_needs_login_and_shows_only_own_entries(
    client: AsyncClient, outbox: list
) -> None:
    alice = await sign_up(client, outbox, "alice@example.com")
    bob = await sign_up(client, outbox, "bob@example.com")
    await create_person(client, alice)

    assert (await client.get("/audit")).status_code == 401
    assert "person_created" not in await actions(client, bob)
    assert "person_created" in await actions(client, alice)


async def test_log_is_paged_newest_first_and_filtered(
    client: AsyncClient, outbox: list
) -> None:
    user = await sign_up(client, outbox, EMAIL)
    for name in ("One", "Two", "Three"):
        await create_person(client, user, name=name)

    page = await entries(client, user, limit=2)
    assert [e["action"] for e in page] == ["person_created", "person_created"]
    times = [e["created_at"] for e in page]
    assert times == sorted(times, reverse=True)
    rest = await entries(client, user, before=page[-1]["created_at"], limit=100)
    assert len(page) + len(rest) == 6
    assert not {e["id"] for e in page} & {e["id"] for e in rest}

    assert len(await entries(client, user, action="person_created")) == 3
    bad = await client.get("/audit", params={"action": "nonsense"}, headers=user)
    assert bad.status_code == 422
    too_many = await client.get("/audit", params={"limit": 101}, headers=user)
    assert too_many.status_code == 422


# ---------- retention ----------


async def test_purge_removes_only_expired_entries(
    db_session: AsyncSession,
) -> None:
    now = datetime.now(UTC)
    for age in (timedelta(days=400), timedelta(days=366), timedelta(days=10)):
        db_session.add(
            AuditEntry(
                actor="anonymous",
                action="login_failed",
                success=False,
                created_at=now - age,
            )
        )
    await db_session.commit()

    assert await audit_service.purge_expired(db_session, now) == 2
    assert await db_session.scalar(select(func.count(AuditEntry.id))) == 1


# ---------- robustness ----------


async def test_failed_audit_write_does_not_break_the_action(
    client: AsyncClient, outbox: list, monkeypatch: pytest.MonkeyPatch
) -> None:
    user = await sign_up(client, outbox, EMAIL)
    agent_id, _ = await set_up_site(client, user)
    camera = await add_camera(client, user, agent_id)
    with monkeypatch.context() as patched:
        # An IP longer than the column makes every audit insert fail.
        patched.setattr(
            context, "current", lambda: context.RequestContext(ip="x" * 100)
        )
        response = await client.patch(
            f"/cameras/{camera['id']}", json={"name": "Renamed"}, headers=user
        )

    assert response.status_code == 200, response.text
    assert response.json()["name"] == "Renamed"
    assert "camera_updated" not in await actions(client, user)
    assert (await client.get(f"/cameras/{camera['id']}", headers=user)).json()[
        "name"
    ] == "Renamed"


def test_context_is_empty_outside_a_request() -> None:
    assert context.current() == context.RequestContext()


async def test_record_without_request_context(db_session: AsyncSession) -> None:
    await audit_service.record(
        db_session,
        audit_service.AuditAction.LOGIN_FAILED,
        user_id=None,
        success=False,
        actor=audit_service.AuditActor.ANONYMOUS,
    )
    entry = await db_session.scalar(select(AuditEntry))
    assert entry.ip is None and entry.user_agent is None
    assert isinstance(entry.id, uuid.UUID)
