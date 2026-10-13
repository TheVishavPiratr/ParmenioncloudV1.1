"""Tests for the media_access module: agent uploads, signed viewing links,
listing, deletion, and retention."""

import hashlib
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from httpx import AsyncClient
from sqlalchemy import update
from sqlalchemy.ext.asyncio import AsyncSession

from api.core import storage
from api.modules.media_access import service
from api.modules.media_access.models import Media
from tests.test_cameras import add_camera
from tests.test_edge_agent import Headers, bearer, enrolled_agent, make_user

SNAPSHOT = b"\xff\xd8\xff\xe0" + b"jpeg-bytes" * 300 + b"\xff\xd9"
CLIP = bytes(range(256)) * 2000


@pytest.fixture(autouse=True)
def media_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Keep every test's files in its own temporary folder."""
    monkeypatch.setattr(storage, "root", tmp_path)
    return tmp_path


# ---------- helpers ----------


def announcement(camera_id: str, content: bytes = SNAPSHOT, **changes) -> dict:
    return {
        "camera_id": camera_id,
        "kind": "snapshot",
        "content_type": "image/jpeg",
        "size_bytes": len(content),
        "sha256": hashlib.sha256(content).hexdigest(),
        "captured_at": datetime.now(UTC).isoformat(),
        **changes,
    }


async def register(client: AsyncClient, token: str, body: dict):
    return await client.post("/agent/media", json=body, headers=bearer(token))


async def put_content(client: AsyncClient, token: str, media_id: str, content: bytes):
    return await client.put(
        f"/agent/media/{media_id}/content", content=content, headers=bearer(token)
    )


async def upload(
    client: AsyncClient,
    token: str,
    camera_id: str,
    content: bytes = SNAPSHOT,
    **changes,
) -> str:
    """Register and upload a file. Returns its media id."""
    registered = await register(
        client, token, announcement(camera_id, content, **changes)
    )
    assert registered.status_code == 201, registered.text
    media_id = registered.json()["media_id"]
    response = await put_content(client, token, media_id, content)
    assert response.status_code == 200, response.text
    return media_id


async def camera_setup(
    client: AsyncClient, db_session: AsyncSession, email: str = "owner@example.com"
) -> tuple[Headers, str, str]:
    """A user with an enrolled agent and one camera. Returns (user, token, camera_id)."""
    user = await make_user(db_session, email)
    agent_id, token = await enrolled_agent(client, user)
    camera = await add_camera(client, user, agent_id)
    return user, token, camera["id"]


async def link(client: AsyncClient, user: Headers, media_id: str) -> str:
    response = await client.post(f"/media/{media_id}/link", headers=user)
    assert response.status_code == 200, response.text
    return response.json()["url"]


# ---------- uploading ----------


async def test_snapshot_upload_flow(
    client: AsyncClient, db_session: AsyncSession, media_root: Path
) -> None:
    user, token, camera_id = await camera_setup(client, db_session)

    registered = await register(client, token, announcement(camera_id))
    body = registered.json()
    uploaded = await put_content(client, token, body["media_id"], SNAPSHOT)

    assert registered.status_code == 201
    assert body["status"] == "pending"
    assert body["upload_url"] == f"/agent/media/{body['media_id']}/content"
    assert uploaded.json()["status"] == "ready"
    [stored] = [p for p in media_root.rglob("*") if p.is_file()]
    assert stored.read_bytes() == SNAPSHOT
    assert stored.suffix == ".jpg"

    media = (await client.get(f"/media/{body['media_id']}", headers=user)).json()
    assert media["camera_id"] == camera_id
    assert media["kind"] == "snapshot"
    assert media["size_bytes"] == len(SNAPSHOT)
    assert media["uploaded_at"] is not None


async def test_clip_upload_with_duration(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    user, token, camera_id = await camera_setup(client, db_session)

    media_id = await upload(
        client,
        token,
        camera_id,
        CLIP,
        kind="clip",
        content_type="video/mp4",
        duration_s=12.5,
    )

    media = (await client.get(f"/media/{media_id}", headers=user)).json()
    assert media["kind"] == "clip"
    assert media["duration_s"] == 12.5


async def test_retries_are_safe(client: AsyncClient, db_session: AsyncSession) -> None:
    _, token, camera_id = await camera_setup(client, db_session)
    body = announcement(camera_id)

    first = (await register(client, token, body)).json()
    again = (await register(client, token, body)).json()
    assert again["media_id"] == first["media_id"]
    assert again["status"] == "pending"

    assert (
        await put_content(client, token, first["media_id"], SNAPSHOT)
    ).status_code == 200
    # Uploading a finished file again, or re-registering it, changes nothing.
    assert (await put_content(client, token, first["media_id"], b"")).status_code == 200
    after = (await register(client, token, body)).json()
    assert after == {**first, "status": "ready"}


async def test_failed_upload_can_be_retried(
    client: AsyncClient, db_session: AsyncSession, media_root: Path
) -> None:
    _, token, camera_id = await camera_setup(client, db_session)
    media_id = (await register(client, token, announcement(camera_id))).json()[
        "media_id"
    ]

    short = await put_content(client, token, media_id, SNAPSHOT[:-1])
    assert short.status_code == 400
    assert [p for p in media_root.rglob("*") if p.is_file()] == []

    retry = await put_content(client, token, media_id, SNAPSHOT)
    assert retry.json()["status"] == "ready"


# ---------- viewing ----------


async def test_signed_link_serves_file_without_login(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    user, token, camera_id = await camera_setup(client, db_session)
    media_id = await upload(client, token, camera_id)

    created = await client.post(f"/media/{media_id}/link", headers=user)
    response = await client.get(created.json()["url"])

    expires_at = datetime.fromisoformat(created.json()["expires_at"])
    assert (
        timedelta(minutes=9) < expires_at - datetime.now(UTC) <= timedelta(minutes=10)
    )
    assert response.status_code == 200
    assert response.content == SNAPSHOT
    assert response.headers["content-type"] == "image/jpeg"
    assert response.headers["cache-control"] == "private, no-store"
    assert response.headers["x-content-type-options"] == "nosniff"
    assert response.headers["content-disposition"] == "inline"


async def test_signed_link_supports_seeking(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    user, token, camera_id = await camera_setup(client, db_session)
    media_id = await upload(
        client, token, camera_id, CLIP, kind="clip", content_type="video/mp4"
    )
    url = await link(client, user, media_id)

    response = await client.get(url, headers={"Range": "bytes=1000-1999"})

    assert response.status_code == 206
    assert response.content == CLIP[1000:2000]
    assert response.headers["content-range"] == f"bytes 1000-1999/{len(CLIP)}"


async def test_unfinished_upload_is_invisible_to_user(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    user, token, camera_id = await camera_setup(client, db_session)
    media_id = (await register(client, token, announcement(camera_id))).json()[
        "media_id"
    ]

    assert (await client.get("/media", headers=user)).json() == []
    assert (await client.get(f"/media/{media_id}", headers=user)).status_code == 404
    assert (
        await client.post(f"/media/{media_id}/link", headers=user)
    ).status_code == 404


# ---------- listing and deleting ----------


async def test_list_filters_and_pages_newest_first(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    user, token, camera_id = await camera_setup(client, db_session)
    now = datetime.now(UTC)
    snapshots = []
    for minutes in (30, 20, 10):
        content = SNAPSHOT + str(minutes).encode()
        captured = (now - timedelta(minutes=minutes)).isoformat()
        snapshots.append(
            await upload(client, token, camera_id, content, captured_at=captured)
        )
    clip = await upload(
        client, token, camera_id, CLIP, kind="clip", content_type="video/mp4"
    )

    clips = (await client.get("/media?kind=clip", headers=user)).json()
    page1 = (await client.get("/media?kind=snapshot&limit=2", headers=user)).json()
    page2 = (
        await client.get(
            "/media",
            params={"kind": "snapshot", "limit": 2, "before": page1[-1]["captured_at"]},
            headers=user,
        )
    ).json()
    by_camera = await client.get(f"/media?camera_id={camera_id}", headers=user)

    assert [m["id"] for m in clips] == [clip]
    assert [m["id"] for m in page1] == [snapshots[2], snapshots[1]]
    assert [m["id"] for m in page2] == [snapshots[0]]
    assert len(by_camera.json()) == 4


async def test_delete_removes_file_and_link(
    client: AsyncClient, db_session: AsyncSession, media_root: Path
) -> None:
    user, token, camera_id = await camera_setup(client, db_session)
    media_id = await upload(client, token, camera_id)
    url = await link(client, user, media_id)

    deleted = await client.delete(f"/media/{media_id}", headers=user)

    assert deleted.status_code == 204
    assert [p for p in media_root.rglob("*") if p.is_file()] == []
    assert (await client.get(f"/media/{media_id}", headers=user)).status_code == 404
    assert (await client.get(url)).status_code == 404


async def test_footage_outlives_its_camera(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    user, token, camera_id = await camera_setup(client, db_session)
    media_id = await upload(client, token, camera_id)

    await client.delete(f"/cameras/{camera_id}", headers=user)

    media = (await client.get(f"/media/{media_id}", headers=user)).json()
    assert media["camera_id"] is None
    assert (await client.get(await link(client, user, media_id))).status_code == 200


# ---------- retention ----------


async def test_retention_removes_old_and_abandoned_media(
    client: AsyncClient, db_session: AsyncSession, media_root: Path
) -> None:
    user, token, camera_id = await camera_setup(client, db_session)
    old = await upload(
        client,
        token,
        camera_id,
        SNAPSHOT + b"old",
        captured_at=(datetime.now(UTC) - timedelta(days=31)).isoformat(),
    )
    fresh = await upload(client, token, camera_id)
    abandoned = (
        await register(client, token, announcement(camera_id, SNAPSHOT + b"x"))
    ).json()["media_id"]
    await db_session.execute(
        update(Media)
        .where(Media.id == uuid.UUID(abandoned))
        .values(created_at=datetime.now(UTC) - timedelta(days=2))
    )
    await db_session.commit()

    assert await service.purge_expired(db_session) == 2
    assert await service.purge_expired(db_session) == 0

    remaining = [m["id"] for m in (await client.get("/media", headers=user)).json()]
    assert remaining == [fresh]
    assert old not in remaining
    assert len([p for p in media_root.rglob("*") if p.is_file()]) == 1
