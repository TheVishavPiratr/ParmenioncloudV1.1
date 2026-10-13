"""Tests for the gallery module: people, reference photos, syncing to agents,
and embeddings shared between agents."""

from pathlib import Path

import pytest
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from api.core import storage
from tests.test_edge_agent import (
    Headers,
    bearer,
    commands_of,
    create_agent,
    enrolled_agent,
    make_user,
)

JPEG = b"\xff\xd8\xff\xe0" + b"face-jpeg" * 400 + b"\xff\xd9"
PNG = b"\x89PNG\r\n\x1a\n" + b"face-png" * 400
WEBP = b"RIFF\x00\x00\x00\x00WEBP" + b"face-webp" * 400
PERSON = {"name": "Asha", "category": "family", "consent_confirmed": True}
MODEL = "arcface-r100"
VECTOR = [0.125] * 128


@pytest.fixture(autouse=True)
def media_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Keep every test's files in its own temporary folder."""
    monkeypatch.setattr(storage, "root", tmp_path)
    return tmp_path


# ---------- helpers ----------


def stored_files(root: Path) -> list[Path]:
    return [p for p in root.rglob("*") if p.is_file()]


async def create_person(client: AsyncClient, user: Headers, **changes) -> dict:
    response = await client.post(
        "/gallery/people", json={**PERSON, **changes}, headers=user
    )
    assert response.status_code == 201, response.text
    return response.json()


async def add_face(
    client: AsyncClient, user: Headers, person_id: str, content: bytes = JPEG
):
    return await client.post(
        f"/gallery/people/{person_id}/faces", content=content, headers=user
    )


async def face(
    client: AsyncClient, user: Headers, person_id: str, content=JPEG
) -> dict:
    response = await add_face(client, user, person_id, content)
    assert response.status_code == 201, response.text
    return response.json()


async def agent_gallery(client: AsyncClient, token: str, model: str = MODEL) -> dict:
    response = await client.get(
        "/agent/gallery", params={"model": model}, headers=bearer(token)
    )
    assert response.status_code == 200, response.text
    return response.json()


async def report(client: AsyncClient, token: str, *results: dict, model: str = MODEL):
    return await client.post(
        "/agent/gallery/embeddings",
        json={"model": model, "results": list(results)},
        headers=bearer(token),
    )


async def config_changes(db_session: AsyncSession, agent_id: str) -> int:
    commands = await commands_of(db_session, agent_id)
    return sum(c.type == "config_changed" for c in commands)


async def owner_with_agent(
    client: AsyncClient, db_session: AsyncSession, email: str = "owner@example.com"
) -> tuple[Headers, str, str]:
    """Returns (user, agent_id, agent_token)."""
    user = await make_user(db_session, email)
    agent_id, token = await enrolled_agent(client, user)
    return user, agent_id, token


# ---------- people ----------


async def test_creating_a_person_requires_consent(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    user = await make_user(db_session)

    missing = await client.post(
        "/gallery/people", json={"name": "A", "category": "family"}, headers=user
    )
    refused = await client.post(
        "/gallery/people", json={**PERSON, "consent_confirmed": False}, headers=user
    )
    created = await create_person(client, user, name="  Asha  ")

    assert missing.status_code == refused.status_code == 422
    assert created["name"] == "Asha"
    assert created["category"] == "family"
    assert created["faces"] == []


async def test_list_people_sorted_and_filtered(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    user = await make_user(db_session)
    await create_person(client, user, name="Zoya", category="staff")
    await create_person(client, user, name="Arjun", category="blocked")
    await create_person(client, user, name="Meera")

    everyone = (await client.get("/gallery/people", headers=user)).json()
    blocked = (
        await client.get("/gallery/people?category=blocked", headers=user)
    ).json()

    assert [p["name"] for p in everyone] == ["Arjun", "Meera", "Zoya"]
    assert [p["name"] for p in blocked] == ["Arjun"]


async def test_update_person(client: AsyncClient, db_session: AsyncSession) -> None:
    user = await make_user(db_session)
    person = await create_person(client, user, notes="Lives upstairs")
    url = f"/gallery/people/{person['id']}"

    updated = await client.patch(
        url, json={"category": "visitor", "notes": None}, headers=user
    )
    null_name = await client.patch(url, json={"name": None}, headers=user)

    assert updated.json()["category"] == "visitor"
    assert updated.json()["notes"] is None
    assert updated.json()["name"] == "Asha"
    assert null_name.status_code == 422


# ---------- photos ----------


@pytest.mark.parametrize(
    ("content", "content_type"),
    [(JPEG, "image/jpeg"), (PNG, "image/png"), (WEBP, "image/webp")],
)
async def test_photo_type_is_read_from_the_file(
    client: AsyncClient,
    db_session: AsyncSession,
    media_root: Path,
    content: bytes,
    content_type: str,
) -> None:
    user = await make_user(db_session)
    person = await create_person(client, user)

    # The header claims something else: the file's own bytes decide.
    response = await client.post(
        f"/gallery/people/{person['id']}/faces",
        content=content,
        headers={**user, "Content-Type": "text/html"},
    )

    assert response.status_code == 201
    body = response.json()
    assert body["content_type"] == content_type
    assert body["status"] == "pending"
    assert body["size_bytes"] == len(content)
    [stored] = stored_files(media_root)
    assert stored.read_bytes() == content
    assert stored.relative_to(media_root).parts[0] == "gallery"


async def test_same_photo_twice_is_stored_once(
    client: AsyncClient, db_session: AsyncSession, media_root: Path
) -> None:
    user = await make_user(db_session)
    person = await create_person(client, user)

    first = await face(client, user, person["id"])
    second = await face(client, user, person["id"])

    assert first["id"] == second["id"]
    assert len(stored_files(media_root)) == 1
    person_view = (
        await client.get(f"/gallery/people/{person['id']}", headers=user)
    ).json()
    assert [f["id"] for f in person_view["faces"]] == [first["id"]]


async def test_signed_link_shows_photo_without_login(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    user = await make_user(db_session)
    person = await create_person(client, user)
    photo = await face(client, user, person["id"], PNG)

    created = await client.post(f"/gallery/faces/{photo['id']}/link", headers=user)
    response = await client.get(created.json()["url"])

    assert response.status_code == 200
    assert response.content == PNG
    assert response.headers["content-type"] == "image/png"
    assert response.headers["cache-control"] == "private, no-store"
    assert response.headers["x-content-type-options"] == "nosniff"


async def test_delete_photo_and_person_remove_files(
    client: AsyncClient, db_session: AsyncSession, media_root: Path
) -> None:
    user, _, token = await owner_with_agent(client, db_session)
    person = await create_person(client, user)
    first = await face(client, user, person["id"])
    await face(client, user, person["id"], PNG)
    await report(client, token, {"face_id": first["id"], "vector": VECTOR})

    deleted_face = await client.delete(f"/gallery/faces/{first['id']}", headers=user)
    assert deleted_face.status_code == 204
    assert len(stored_files(media_root)) == 1

    deleted_person = await client.delete(
        f"/gallery/people/{person['id']}", headers=user
    )
    assert deleted_person.status_code == 204
    assert stored_files(media_root) == []
    assert (
        await client.get(f"/gallery/people/{person['id']}", headers=user)
    ).status_code == 404
    assert (await agent_gallery(client, token))["people"] == []


# ---------- syncing to agents ----------


async def test_agent_computes_embedding_once_and_gets_it_back(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    user, _, token = await owner_with_agent(client, db_session)
    person = await create_person(client, user, category="staff")
    photo = await face(client, user, person["id"])

    before = await agent_gallery(client, token)
    [agent_person] = before["people"]
    [agent_face] = agent_person["faces"]
    image = await client.get(agent_face["image_url"], headers=bearer(token))
    stored = await report(client, token, {"face_id": photo["id"], "vector": VECTOR})
    after = await agent_gallery(client, token)

    assert before["model"] == MODEL
    assert agent_person == {
        "id": person["id"],
        "name": "Asha",
        "category": "staff",
        "faces": [
            {
                "id": photo["id"],
                "embedding": None,
                "image_url": f"/agent/gallery/faces/{photo['id']}/image",
            }
        ],
    }
    assert image.content == JPEG
    assert stored.json() == {"stored": 1, "rejected": 0, "skipped": 0}
    assert after["people"][0]["faces"] == [
        {"id": photo["id"], "embedding": VECTOR, "image_url": None}
    ]
    person_view = (
        await client.get(f"/gallery/people/{person['id']}", headers=user)
    ).json()
    assert person_view["faces"][0]["status"] == "ready"


async def test_embedding_is_shared_with_other_agents_of_same_model(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    user, first_id, first_token = await owner_with_agent(client, db_session)
    second_id, second_token = await enrolled_agent(client, user)
    person = await create_person(client, user)
    photo = await face(client, user, person["id"])
    first_before = await config_changes(db_session, first_id)
    second_before = await config_changes(db_session, second_id)

    await report(client, first_token, {"face_id": photo["id"], "vector": VECTOR})

    shared = (await agent_gallery(client, second_token))["people"][0]["faces"][0]
    assert shared["embedding"] == VECTOR
    # The second agent is told to re-fetch; the reporting agent needs no telling.
    assert await config_changes(db_session, second_id) == second_before + 1
    assert await config_changes(db_session, first_id) == first_before
    # A different face model cannot reuse these vectors.
    other_model = (await agent_gallery(client, second_token, "facenet-512"))["people"]
    assert other_model[0]["faces"][0]["embedding"] is None
    # A second answer for the same photo and model does not replace the first.
    again = await report(
        client, second_token, {"face_id": photo["id"], "vector": [0.5] * 128}
    )
    assert again.json() == {"stored": 0, "rejected": 0, "skipped": 1}


async def test_photo_without_a_face_is_rejected(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    user, _, token = await owner_with_agent(client, db_session)
    person = await create_person(client, user)
    blurry = await face(client, user, person["id"])
    good = await face(client, user, person["id"], PNG)
    await report(client, token, {"face_id": good["id"], "vector": VECTOR})

    response = await report(
        client,
        token,
        {"face_id": blurry["id"], "rejected_reason": "no face found"},
        # A photo another agent already found a face in stays usable.
        {"face_id": good["id"], "rejected_reason": "no face found"},
    )
    late_vector = await report(
        client, token, {"face_id": blurry["id"], "vector": VECTOR}
    )

    assert response.json() == {"stored": 0, "rejected": 1, "skipped": 1}
    assert late_vector.json() == {"stored": 0, "rejected": 0, "skipped": 1}
    faces = (await client.get(f"/gallery/people/{person['id']}", headers=user)).json()[
        "faces"
    ]
    statuses = {f["id"]: (f["status"], f["reject_reason"]) for f in faces}
    assert statuses[blurry["id"]] == ("rejected", "no face found")
    assert statuses[good["id"]] == ("ready", None)
    agent_faces = (await agent_gallery(client, token))["people"][0]["faces"]
    assert [f["id"] for f in agent_faces] == [good["id"]]


async def test_agents_are_told_only_about_changes_they_use(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    user, agent_id, _ = await owner_with_agent(client, db_session)
    pending_id = (await create_agent(client, user, "Not enrolled"))["agent"]["id"]

    person = await create_person(client, user)
    await client.patch(
        f"/gallery/people/{person['id']}", json={"name": "Asha K"}, headers=user
    )
    assert await config_changes(db_session, agent_id) == 0  # no photos yet

    await face(client, user, person["id"])
    assert await config_changes(db_session, agent_id) == 1
    await client.patch(
        f"/gallery/people/{person['id']}", json={"notes": "cloud only"}, headers=user
    )
    assert await config_changes(db_session, agent_id) == 1
    await client.patch(
        f"/gallery/people/{person['id']}", json={"category": "staff"}, headers=user
    )
    assert await config_changes(db_session, agent_id) == 2
    await client.delete(f"/gallery/people/{person['id']}", headers=user)
    assert await config_changes(db_session, agent_id) == 3
    assert await config_changes(db_session, pending_id) == 0
