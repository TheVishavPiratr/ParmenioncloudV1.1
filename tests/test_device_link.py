"""Tests for the device_link module (phone logins that last, refresh-token
rotation, logging devices out, push tokens) and the auth changes it relies on
(device id in the token, password reset logging out everywhere, token claims)."""

import uuid
from datetime import UTC, datetime, timedelta

import pytest
from httpx import AsyncClient
from sqlalchemy import update
from sqlalchemy.ext.asyncio import AsyncSession

from api.modules.auth import service as auth_service
from api.modules.device_link import service
from api.modules.device_link.models import LinkedDevice
from tests.test_auth import last_code
from tests.test_edge_agent import Headers, bearer
from tests.test_end_to_end import sign_up

EMAIL = "phone.owner@example.com"
PASSWORD = "correct-horse-battery"
PHONE = {
    "name": "Pixel 8",
    "platform": "android",
    "app_version": "1.4.0",
    "os_version": "Android 15",
}
FCM_TOKEN = "fGx1:APA91bH-example_token-123"


# ---------- helpers ----------


async def phone_login(
    client: AsyncClient, email: str = EMAIL, password: str = PASSWORD, **device
):
    return await client.post(
        "/devices/login",
        json={"email": email, "password": password, "device": {**PHONE, **device}},
    )


async def linked_phone(client: AsyncClient, email: str = EMAIL, **device) -> dict:
    """Log in from a phone. Returns the token pair."""
    response = await phone_login(client, email, **device)
    assert response.status_code == 200, response.text
    return response.json()


async def refresh(client: AsyncClient, refresh_token: str):
    return await client.post("/devices/refresh", json={"refresh_token": refresh_token})


async def devices(client: AsyncClient, user: Headers) -> list[dict]:
    response = await client.get("/devices", headers=user)
    assert response.status_code == 200, response.text
    return response.json()


async def owner(client: AsyncClient, outbox: list, email: str = EMAIL) -> Headers:
    """A verified account with PASSWORD, logged in on the web (no device)."""
    return await sign_up(client, outbox, email)


# ---------- logging in ----------


async def test_phone_login_links_device_and_tokens_work(
    client: AsyncClient, outbox: list
) -> None:
    web = await owner(client, outbox)

    pair = await linked_phone(client)
    phone = bearer(pair["access_token"])

    assert pair["token_type"] == "bearer"
    assert pair["refresh_token"].startswith("vpr_")
    assert (await client.get("/auth/me", headers=phone)).json()["email"] == EMAIL
    assert (await client.get("/cameras", headers=phone)).status_code == 200
    # The phone sees itself as the current device; the web login does not.
    [seen_by_phone] = await devices(client, phone)
    [seen_by_web] = await devices(client, web)
    assert seen_by_phone["id"] == pair["device_id"]
    assert seen_by_phone["current"] is True
    assert seen_by_web["current"] is False
    assert seen_by_phone["name"] == "Pixel 8"
    assert seen_by_phone["platform"] == "android"
    assert seen_by_phone["has_push_token"] is False


async def test_login_is_case_insensitive_and_errors_match_auth_login(
    client: AsyncClient, outbox: list
) -> None:
    await owner(client, outbox)
    await client.post(
        "/auth/signup",
        json={
            "email": "unverified@example.com",
            "password": PASSWORD,
            "full_name": "U",
        },
    )

    assert (await phone_login(client, EMAIL.upper())).status_code == 200
    wrong = await phone_login(client, password="wrong-password")
    unknown = await phone_login(client, "nobody@example.com")
    unverified = await phone_login(client, "unverified@example.com")

    assert wrong.status_code == unknown.status_code == 401
    assert wrong.json() == unknown.json() == {"detail": "Incorrect email or password"}
    assert unverified.status_code == 403
    assert unverified.json() == {"detail": "Email is not verified"}


# ---------- staying logged in ----------


async def test_refresh_swaps_the_refresh_token_every_time(
    client: AsyncClient, outbox: list
) -> None:
    await owner(client, outbox)
    first = await linked_phone(client)

    second = (await refresh(client, first["refresh_token"])).json()
    third = (await refresh(client, second["refresh_token"])).json()

    tokens = {first["refresh_token"], second["refresh_token"], third["refresh_token"]}
    assert len(tokens) == 3
    assert first["device_id"] == second["device_id"] == third["device_id"]
    me = await client.get("/auth/me", headers=bearer(third["access_token"]))
    assert me.status_code == 200


async def test_reused_refresh_token_logs_the_device_out(
    client: AsyncClient, outbox: list
) -> None:
    """A swapped-out token coming back means it was copied: the thief and the
    real phone are both logged out, and the owner simply logs in again."""
    web = await owner(client, outbox)
    first = await linked_phone(client)
    second = (await refresh(client, first["refresh_token"])).json()

    stolen = await refresh(client, first["refresh_token"])
    real_phone = await refresh(client, second["refresh_token"])

    assert stolen.status_code == real_phone.status_code == 401
    assert stolen.json() == {"detail": "Session expired; log in again"}
    assert await devices(client, web) == []
    assert (await phone_login(client)).status_code == 200


async def test_expired_refresh_token_is_refused(
    client: AsyncClient, outbox: list, db_session: AsyncSession
) -> None:
    web = await owner(client, outbox)
    pair = await linked_phone(client)
    await db_session.execute(
        update(LinkedDevice)
        .where(LinkedDevice.id == uuid.UUID(pair["device_id"]))
        .values(refresh_expires_at=datetime.now(UTC) - timedelta(seconds=1))
    )
    await db_session.commit()

    assert (await refresh(client, pair["refresh_token"])).status_code == 401
    assert await devices(client, web) == []


async def test_disabled_account_cannot_refresh(
    client: AsyncClient, outbox: list, db_session: AsyncSession
) -> None:
    await owner(client, outbox)
    pair = await linked_phone(client)
    user = await auth_service.get_user_from_token(db_session, pair["access_token"])
    user.is_active = False
    await db_session.commit()

    assert (await refresh(client, pair["refresh_token"])).status_code == 401


# ---------- logging out ----------


async def test_logout_unlinks_this_device(client: AsyncClient, outbox: list) -> None:
    web = await owner(client, outbox)
    pair = await linked_phone(client)

    response = await client.post(
        "/devices/logout", json={"refresh_token": pair["refresh_token"]}
    )
    again = await client.post(
        "/devices/logout", json={"refresh_token": pair["refresh_token"]}
    )

    assert response.status_code == again.status_code == 204
    assert (await refresh(client, pair["refresh_token"])).status_code == 401
    assert await devices(client, web) == []


async def test_lost_phone_is_logged_out_remotely(
    client: AsyncClient, outbox: list
) -> None:
    web = await owner(client, outbox)
    lost = await linked_phone(client, name="Lost phone")
    kept = await linked_phone(client, name="Tablet", platform="ios")

    removed = await client.delete(f"/devices/{lost['device_id']}", headers=web)

    assert removed.status_code == 204
    assert (await refresh(client, lost["refresh_token"])).status_code == 401
    assert (await refresh(client, kept["refresh_token"])).status_code == 200
    assert [d["name"] for d in await devices(client, web)] == ["Tablet"]


async def test_logout_all_keeps_the_device_asking(
    client: AsyncClient, outbox: list
) -> None:
    web = await owner(client, outbox)
    asking = await linked_phone(client, name="Mine")
    others = [await linked_phone(client, name=f"Other {i}") for i in range(2)]

    response = await client.post(
        "/devices/logout-all", headers=bearer(asking["access_token"])
    )

    assert response.json() == {"logged_out": 2}
    assert (await refresh(client, asking["refresh_token"])).status_code == 200
    for other in others:
        assert (await refresh(client, other["refresh_token"])).status_code == 401

    # From the web (not a linked device) it logs out every device.
    everywhere = await client.post("/devices/logout-all", headers=web)
    assert everywhere.json() == {"logged_out": 1}
    assert await devices(client, web) == []


async def test_oldest_unused_device_makes_room(
    client: AsyncClient,
    outbox: list,
    db_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(service, "MAX_DEVICES_PER_USER", 2)
    web = await owner(client, outbox)
    old = await linked_phone(client, name="Old")
    recent = await linked_phone(client, name="Recent")
    await db_session.execute(
        update(LinkedDevice)
        .where(LinkedDevice.id == uuid.UUID(old["device_id"]))
        .values(last_seen_at=datetime.now(UTC) - timedelta(days=30))
    )
    await db_session.commit()

    await linked_phone(client, name="New")

    assert sorted(d["name"] for d in await devices(client, web)) == ["New", "Recent"]
    assert (await refresh(client, old["refresh_token"])).status_code == 401
    assert (await refresh(client, recent["refresh_token"])).status_code == 200


# ---------- device settings and push tokens ----------


async def test_rename_and_switch_notifications(
    client: AsyncClient, outbox: list
) -> None:
    web = await owner(client, outbox)
    pair = await linked_phone(client)
    url = f"/devices/{pair['device_id']}"

    renamed = await client.patch(
        url,
        json={"name": "  Work phone  ", "notifications_enabled": False},
        headers=web,
    )
    null_name = await client.patch(url, json={"name": None}, headers=web)

    assert renamed.json()["name"] == "Work phone"
    assert renamed.json()["notifications_enabled"] is False
    assert null_name.status_code == 422


async def test_push_tokens_reach_notifications(
    client: AsyncClient, outbox: list, db_session: AsyncSession
) -> None:
    web = await owner(client, outbox)
    phone = await linked_phone(client)
    muted = await linked_phone(client, name="Muted tablet")
    user_id = auth_service.get_token_claims(phone["access_token"]).user_id

    registered = await client.put(
        f"/devices/{phone['device_id']}/push-token",
        json={"provider": "fcm", "token": FCM_TOKEN},
        headers=web,
    )
    await client.put(
        f"/devices/{muted['device_id']}/push-token",
        json={"provider": "fcm", "token": "muted-token"},
        headers=web,
    )
    await client.patch(
        f"/devices/{muted['device_id']}",
        json={"notifications_enabled": False},
        headers=web,
    )

    assert registered.json()["has_push_token"] is True
    targets = await service.list_push_targets(db_session, user_id)
    assert [(str(t.device_id), t.provider, t.token) for t in targets] == [
        (phone["device_id"], "fcm", FCM_TOKEN)
    ]

    # Firebase says the token is dead: notifications drops it.
    await service.drop_push_token(db_session, "fcm", FCM_TOKEN)
    assert await service.list_push_targets(db_session, user_id) == []

    cleared = await client.delete(
        f"/devices/{muted['device_id']}/push-token", headers=web
    )
    assert cleared.json()["has_push_token"] is False


async def test_push_token_follows_the_phone_to_a_new_account(
    client: AsyncClient, outbox: list
) -> None:
    alice = await owner(client, outbox, "alice.phone@example.com")
    bob = await owner(client, outbox, "bob.phone@example.com")
    on_alice = await linked_phone(client, "alice.phone@example.com")
    on_bob = await linked_phone(client, "bob.phone@example.com")

    for device, user in ((on_alice, alice), (on_bob, bob)):
        response = await client.put(
            f"/devices/{device['device_id']}/push-token",
            json={"provider": "fcm", "token": FCM_TOKEN},
            headers=user,
        )
        assert response.status_code == 200

    # Alice no longer gets alerts on a phone that now belongs to Bob's login.
    [alice_device] = await devices(client, alice)
    [bob_device] = await devices(client, bob)
    assert alice_device["has_push_token"] is False
    assert bob_device["has_push_token"] is True


# ---------- the three auth changes ----------


async def test_change_1_device_tokens_carry_the_device_id(
    client: AsyncClient, outbox: list
) -> None:
    web = await owner(client, outbox)
    pair = await linked_phone(client)

    phone_claims = auth_service.get_token_claims(pair["access_token"])
    web_claims = auth_service.get_token_claims(web["Authorization"].split()[1])

    assert str(phone_claims.device_id) == pair["device_id"]
    assert web_claims.device_id is None
    assert phone_claims.user_id == web_claims.user_id
    assert phone_claims.session_version == web_claims.session_version == 0


async def test_change_2_password_reset_logs_out_everywhere(
    client: AsyncClient, outbox: list
) -> None:
    web = await owner(client, outbox)
    phone = await linked_phone(client)

    await client.post("/auth/forgot-password", json={"email": EMAIL})
    reset = await client.post(
        "/auth/reset-password",
        json={
            "email": EMAIL,
            "code": last_code(outbox),
            "new_password": "new-pass-123",
        },
    )
    assert reset.status_code == 200

    # Every old login stops working: web token, phone token, phone session.
    assert (await client.get("/auth/me", headers=web)).status_code == 401
    phone_me = await client.get("/auth/me", headers=bearer(phone["access_token"]))
    assert phone_me.status_code == 401
    assert (await refresh(client, phone["refresh_token"])).status_code == 401
    # Logging in again right away, in the same second, works.
    new_web = await client.post(
        "/auth/login", json={"email": EMAIL, "password": "new-pass-123"}
    )
    new_phone = await phone_login(client, password="new-pass-123")
    assert (
        await client.get("/auth/me", headers=bearer(new_web.json()["access_token"]))
    ).status_code == 200
    assert (await refresh(client, new_phone.json()["refresh_token"])).status_code == 200
    assert (
        auth_service.get_token_claims(new_web.json()["access_token"]).session_version
        == 1
    )


async def test_change_3_token_claims_reject_bad_tokens(
    client: AsyncClient, outbox: list
) -> None:
    import jwt

    from api.core.config import settings

    web = await owner(client, outbox)
    claims = auth_service.get_token_claims(web["Authorization"].split()[1])
    now = datetime.now(UTC)
    good = {
        "sub": str(claims.user_id),
        "iat": now,
        "exp": now + timedelta(minutes=5),
    }

    def signed(**changes) -> str:
        payload = {k: v for k, v in {**good, **changes}.items() if v is not None}
        return jwt.encode(payload, settings.jwt_secret, algorithm="HS256")

    for token in (
        signed(sv="0"),  # session version must be a number
        signed(sv=True),
        signed(did="not-a-uuid"),
        signed(iat=None),  # issue time is required now
        signed(exp=now - timedelta(seconds=1)),
        jwt.encode(good, "a-wrong-secret-that-is-long-enough-32b", algorithm="HS256"),
        "not.a.token",
    ):
        with pytest.raises(auth_service.InvalidTokenError):
            auth_service.get_token_claims(token)
        assert (await client.get("/auth/me", headers=bearer(token))).status_code == 401
    # A token without sv (issued before this change) counts as version 0.
    assert (await client.get("/auth/me", headers=bearer(signed()))).status_code == 200
