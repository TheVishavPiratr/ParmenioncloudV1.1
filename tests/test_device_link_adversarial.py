"""Hostile and edge-case tests for the device_link module: users reaching for
each other's devices, token confusion, and bad input."""

import uuid

import pytest
from httpx import AsyncClient

from tests.test_device_link import (
    EMAIL,
    PASSWORD,
    PHONE,
    devices,
    linked_phone,
    owner,
    phone_login,
    refresh,
)
from tests.test_edge_agent import bearer


async def test_users_cannot_touch_each_others_devices(
    client: AsyncClient, outbox: list
) -> None:
    alice = await owner(client, outbox, "alice.dev@example.com")
    bob = await owner(client, outbox, "bob.dev@example.com")
    phone = await linked_phone(client, "alice.dev@example.com")
    url = f"/devices/{phone['device_id']}"

    assert await devices(client, bob) == []
    assert (await client.patch(url, json={"name": "x"}, headers=bob)).status_code == 404
    assert (await client.delete(url, headers=bob)).status_code == 404
    push = await client.put(
        f"{url}/push-token", json={"provider": "fcm", "token": "bobs"}, headers=bob
    )
    assert push.status_code == 404
    assert (await client.delete(f"{url}/push-token", headers=bob)).status_code == 404
    # Bob's "log out everywhere" does not reach Alice.
    await client.post("/devices/logout-all", headers=bob)
    assert (await refresh(client, phone["refresh_token"])).status_code == 200
    assert len(await devices(client, alice)) == 1


async def test_tokens_cannot_stand_in_for_each_other(
    client: AsyncClient, outbox: list
) -> None:
    await owner(client, outbox)
    pair = await linked_phone(client)

    # An access token is not a refresh token, and the other way round.
    assert (await refresh(client, pair["access_token"])).status_code in (401, 422)
    me = await client.get("/auth/me", headers=bearer(pair["refresh_token"]))
    assert me.status_code == 401
    for token in (
        "vpr_" + "x" * 43,
        "vpa_" + pair["refresh_token"][4:],  # right secret, agent prefix
        "vpe_whatever",
        "",
    ):
        response = await refresh(client, token)
        assert response.status_code in (401, 422), token
    # None of those guesses hurt the real session.
    assert (await refresh(client, pair["refresh_token"])).status_code == 200


async def test_logout_with_unknown_token_reveals_nothing(client: AsyncClient) -> None:
    response = await client.post(
        "/devices/logout", json={"refresh_token": "vpr_unknown"}
    )

    assert response.status_code == 204


async def test_management_endpoints_need_login(client: AsyncClient) -> None:
    device_id = uuid.uuid4()
    for method, path in (
        ("GET", "/devices"),
        ("POST", "/devices/logout-all"),
        ("PATCH", f"/devices/{device_id}"),
        ("DELETE", f"/devices/{device_id}"),
        ("PUT", f"/devices/{device_id}/push-token"),
    ):
        response = await client.request(method, path, json={})
        assert response.status_code == 401, (method, path)


@pytest.mark.parametrize(
    "device",
    [
        {**PHONE, "platform": "windows"},
        {**PHONE, "name": ""},
        {**PHONE, "name": "   "},
        {**PHONE, "name": "x" * 101},
        {**PHONE, "name": "bad\x00name"},
        {**PHONE, "name": "two\nlines"},
        {**PHONE, "app_version": "v" * 65},
        {**PHONE, "os_version": "a\x07b"},
        {"platform": "android"},
    ],
)
async def test_bad_device_info_is_rejected(
    client: AsyncClient, outbox: list, device: dict
) -> None:
    await owner(client, outbox)

    response = await client.post(
        "/devices/login",
        json={"email": EMAIL, "password": PASSWORD, "device": device},
    )

    assert response.status_code == 422


@pytest.mark.parametrize(
    "body",
    [
        {"provider": "apns", "token": "abc"},
        {"provider": "fcm", "token": ""},
        {"provider": "fcm", "token": "has space"},
        {"provider": "fcm", "token": "<script>alert(1)</script>"},
        {"provider": "fcm", "token": "a" * 513},
        {"provider": "fcm"},
    ],
)
async def test_bad_push_tokens_are_rejected(
    client: AsyncClient, outbox: list, body: dict
) -> None:
    web = await owner(client, outbox)
    pair = await linked_phone(client)

    response = await client.put(
        f"/devices/{pair['device_id']}/push-token", json=body, headers=web
    )

    assert response.status_code == 422


async def test_bad_refresh_requests_are_rejected(client: AsyncClient) -> None:
    for body in ({}, {"refresh_token": "x" * 201}, {"refresh_token": "vpr_\x00"}):
        assert (await client.post("/devices/refresh", json=body)).status_code == 422, (
            body
        )


async def test_wrong_password_does_not_link_a_device(
    client: AsyncClient, outbox: list
) -> None:
    web = await owner(client, outbox)

    for _ in range(3):
        assert (await phone_login(client, password="guess")).status_code == 401

    assert await devices(client, web) == []


async def test_malformed_device_ids(client: AsyncClient, outbox: list) -> None:
    web = await owner(client, outbox)

    assert (
        await client.patch("/devices/not-a-uuid", json={}, headers=web)
    ).status_code == 422
    assert (
        await client.delete(f"/devices/{uuid.uuid4()}", headers=web)
    ).status_code == 404
