"""Hostile and edge-case tests for the media_access module: people and agents
reaching for each other's footage, forged links, and broken uploads."""

import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import pytest
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from api.core import signed_links, storage
from api.modules.media_access import service
from tests.test_cameras import discovered_camera
from tests.test_edge_agent import bearer, make_user
from tests.test_media_access import (
    SNAPSHOT,
    announcement,
    camera_setup,
    link,
    put_content,
    register,
    upload,
)


@pytest.fixture(autouse=True)
def media_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setattr(storage, "root", tmp_path)
    return tmp_path


def stored_files(root: Path) -> list[Path]:
    return [p for p in root.rglob("*") if p.is_file()]


# ---------- other people's footage ----------


async def test_users_cannot_reach_each_others_media(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    alice, token, camera_id = await camera_setup(
        client, db_session, "alice@example.com"
    )
    bob = await make_user(db_session, "bob@example.com")
    media_id = await upload(client, token, camera_id)

    assert (await client.get("/media", headers=bob)).json() == []
    assert (await client.get(f"/media/{media_id}", headers=bob)).status_code == 404
    assert (
        await client.post(f"/media/{media_id}/link", headers=bob)
    ).status_code == 404
    assert (await client.delete(f"/media/{media_id}", headers=bob)).status_code == 404
    assert (await client.get(f"/media/{media_id}", headers=alice)).status_code == 200


async def test_agent_cannot_upload_for_another_agents_camera(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    _, alice_token, alice_camera = await camera_setup(
        client, db_session, "alice@example.com"
    )
    _, bob_token, _ = await camera_setup(client, db_session, "bob@example.com")
    alice_media = (
        await register(client, alice_token, announcement(alice_camera))
    ).json()

    onto_alices_camera = await register(client, bob_token, announcement(alice_camera))
    into_alices_upload = await put_content(
        client, bob_token, alice_media["media_id"], SNAPSHOT
    )

    assert onto_alices_camera.status_code == 404
    assert into_alices_upload.status_code == 404


async def test_upload_needs_an_agent_token(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    user, _, camera_id = await camera_setup(client, db_session)
    body = announcement(camera_id)

    assert (
        await client.post("/agent/media", json=body, headers=user)
    ).status_code == 401
    assert (await client.post("/agent/media", json=body)).status_code == 401


async def test_revoked_agent_cannot_upload(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    user, token, camera_id = await camera_setup(client, db_session)
    agent_id = (await client.get("/agents", headers=user)).json()[0]["id"]
    media_id = (await register(client, token, announcement(camera_id))).json()[
        "media_id"
    ]
    await client.post(f"/agents/{agent_id}/revoke", headers=user)

    assert (await register(client, token, announcement(camera_id))).status_code == 401
    assert (await put_content(client, token, media_id, SNAPSHOT)).status_code == 401


async def test_only_set_up_cameras_accept_media(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    user, token, camera_id = await camera_setup(client, db_session)
    found = await discovered_camera(client, user, token)
    await client.patch(f"/cameras/{camera_id}", json={"enabled": False}, headers=user)

    assert (await register(client, token, announcement(found["id"]))).status_code == 404
    # Footage from just before a camera was switched off is still kept.
    assert (await register(client, token, announcement(camera_id))).status_code == 201


# ---------- forged and expired links ----------


async def test_tampered_or_expired_links_are_refused(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    user, token, camera_id = await camera_setup(client, db_session)
    media_id = await upload(client, token, camera_id)
    other_id = await upload(client, token, camera_id, SNAPSHOT + b"other")
    url = await link(client, user, media_id)
    query = parse_qs(urlsplit(url).query)
    exp, sig = int(query["exp"][0]), query["sig"][0]
    past = int((datetime.now(UTC) - timedelta(seconds=1)).timestamp())

    for bad in (
        f"/media/files/{media_id}?exp={exp}&sig={'0' * 64}",
        f"/media/files/{media_id}?exp={exp + 3600}&sig={sig}",
        f"/media/files/{other_id}?exp={exp}&sig={sig}",
        f"/media/files/{media_id}?exp={past}&sig={signed_links.sign('media', uuid.UUID(media_id), past)}",
        f"/media/files/{media_id}?exp={exp}&sig={sig.upper()}",
    ):
        assert (await client.get(bad)).status_code == 403, bad
    assert (await client.get(url)).status_code == 200


async def test_malformed_link_parameters(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    media_id = uuid.uuid4()
    for bad in (
        f"/media/files/{media_id}",
        f"/media/files/{media_id}?exp=soon&sig=abc",
        f"/media/files/{media_id}?exp=1&sig={'a' * 65}",
        "/media/files/not-a-uuid?exp=1&sig=abc",
    ):
        assert (await client.get(bad)).status_code == 422, bad


async def test_file_missing_from_storage_is_404_not_500(
    client: AsyncClient, db_session: AsyncSession, media_root: Path
) -> None:
    user, token, camera_id = await camera_setup(client, db_session)
    media_id = await upload(client, token, camera_id)
    for path in stored_files(media_root):
        path.unlink()

    assert (await client.get(await link(client, user, media_id))).status_code == 404


# ---------- broken uploads ----------


async def test_corrupted_upload_is_rejected_and_leaves_nothing(
    client: AsyncClient, db_session: AsyncSession, media_root: Path
) -> None:
    user, token, camera_id = await camera_setup(client, db_session)
    media_id = (await register(client, token, announcement(camera_id))).json()[
        "media_id"
    ]
    flipped = bytes([SNAPSHOT[0] ^ 1]) + SNAPSHOT[1:]

    same_length_wrong_bytes = await put_content(client, token, media_id, flipped)
    too_long = await put_content(client, token, media_id, SNAPSHOT + b"!")
    empty = await put_content(client, token, media_id, b"")

    assert same_length_wrong_bytes.status_code == 400
    assert too_long.status_code == 413
    assert empty.status_code == 400
    assert stored_files(media_root) == []  # no partial or temp files
    assert (await client.get("/media", headers=user)).json() == []


async def test_unknown_media_id_upload(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    _, token, _ = await camera_setup(client, db_session)

    response = await put_content(client, token, str(uuid.uuid4()), SNAPSHOT)

    assert response.status_code == 404


async def test_pending_upload_limit(
    client: AsyncClient, db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(service, "MAX_PENDING_PER_AGENT", 2)
    _, token, camera_id = await camera_setup(client, db_session)
    first = announcement(camera_id, SNAPSHOT + b"1")
    await register(client, token, first)
    await register(client, token, announcement(camera_id, SNAPSHOT + b"2"))

    third = await register(client, token, announcement(camera_id, SNAPSHOT + b"3"))
    # Re-registering a file already on record is still answered.
    retry = await register(client, token, first)

    assert third.status_code == 429
    assert retry.status_code == 201


async def test_uppercase_hash_is_accepted(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    _, token, camera_id = await camera_setup(client, db_session)
    body = announcement(camera_id)
    body["sha256"] = body["sha256"].upper()

    media_id = (await register(client, token, body)).json()["media_id"]

    assert (await put_content(client, token, media_id, SNAPSHOT)).status_code == 200


@pytest.mark.parametrize(
    "changes",
    [
        {"content_type": "video/mp4"},
        {"content_type": "text/html"},
        {"content_type": "image/svg+xml"},
        {"kind": "clip", "content_type": "image/jpeg"},
        {"kind": "document"},
        {"size_bytes": 0},
        {"size_bytes": -1},
        {"size_bytes": 10 * 1024 * 1024 + 1},
        {
            "kind": "clip",
            "content_type": "video/mp4",
            "size_bytes": 200 * 1024 * 1024 + 1,
        },
        {"sha256": "z" * 64},
        {"sha256": "a" * 63},
        {"captured_at": "2026-10-01T12:00:00"},
        {"captured_at": (datetime.now(UTC) + timedelta(hours=1)).isoformat()},
        {"duration_s": 5},
        {"kind": "clip", "content_type": "video/mp4", "duration_s": 0},
        {"kind": "clip", "content_type": "video/mp4", "duration_s": 3601},
        {"camera_id": "not-a-uuid"},
    ],
)
async def test_bad_registrations_are_rejected(
    client: AsyncClient, db_session: AsyncSession, changes: dict
) -> None:
    _, token, camera_id = await camera_setup(client, db_session)

    response = await register(client, token, {**announcement(camera_id), **changes})

    assert response.status_code == 422


async def test_list_rejects_bad_parameters(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    user = await make_user(db_session)

    for query in ("limit=0", "limit=101", "kind=document", "before=yesterday"):
        assert (await client.get(f"/media?{query}", headers=user)).status_code == 422
    assert (await client.get("/media")).status_code == 401
    assert (await client.get("/media", headers=bearer("vpa_x"))).status_code == 401
