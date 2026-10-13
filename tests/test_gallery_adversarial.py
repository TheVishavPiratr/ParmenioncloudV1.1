"""Hostile and edge-case tests for the gallery module: people and agents
reaching for each other's faces, fake images, forged links, and bad vectors."""

import uuid
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import pytest
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from api.core import signed_links, storage
from api.modules.gallery import service
from tests.test_edge_agent import bearer, make_user
from tests.test_gallery import (
    JPEG,
    MODEL,
    PERSON,
    VECTOR,
    add_face,
    agent_gallery,
    create_person,
    face,
    owner_with_agent,
    report,
    stored_files,
)


@pytest.fixture(autouse=True)
def media_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setattr(storage, "root", tmp_path)
    return tmp_path


# ---------- other people's faces ----------


async def test_users_cannot_reach_each_others_gallery(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    alice = await make_user(db_session, "alice@example.com")
    bob = await make_user(db_session, "bob@example.com")
    person = await create_person(client, alice)
    photo = await face(client, alice, person["id"])
    person_url = f"/gallery/people/{person['id']}"

    assert (await client.get("/gallery/people", headers=bob)).json() == []
    assert (await client.get(person_url, headers=bob)).status_code == 404
    assert (
        await client.patch(person_url, json={"name": "x"}, headers=bob)
    ).status_code == 404
    assert (await add_face(client, bob, person["id"])).status_code == 404
    assert (
        await client.post(f"/gallery/faces/{photo['id']}/link", headers=bob)
    ).status_code == 404
    assert (
        await client.delete(f"/gallery/faces/{photo['id']}", headers=bob)
    ).status_code == 404
    assert (await client.delete(person_url, headers=bob)).status_code == 404
    assert (await client.get(person_url, headers=alice)).json()["name"] == "Asha"


async def test_agents_only_see_their_owners_gallery(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    alice, _, alice_token = await owner_with_agent(
        client, db_session, "alice@example.com"
    )
    _, _, bob_token = await owner_with_agent(client, db_session, "bob@example.com")
    person = await create_person(client, alice)
    photo = await face(client, alice, person["id"])

    image = await client.get(
        f"/agent/gallery/faces/{photo['id']}/image", headers=bearer(bob_token)
    )
    poisoned = await report(
        client, bob_token, {"face_id": photo["id"], "vector": VECTOR}
    )

    assert (await agent_gallery(client, bob_token))["people"] == []
    assert image.status_code == 404
    assert poisoned.json() == {"stored": 0, "rejected": 0, "skipped": 1}
    alice_face = (await agent_gallery(client, alice_token))["people"][0]["faces"][0]
    assert alice_face["embedding"] is None


async def test_agent_endpoints_need_an_agent_token(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    user, agent_id, token = await owner_with_agent(client, db_session)
    params = {"model": MODEL}

    assert (
        await client.get("/agent/gallery", params=params, headers=user)
    ).status_code == 401
    assert (await client.get("/agent/gallery", params=params)).status_code == 401
    await client.post(f"/agents/{agent_id}/revoke", headers=user)
    assert (
        await client.get("/agent/gallery", params=params, headers=bearer(token))
    ).status_code == 401


# ---------- fake and oversized photos ----------


@pytest.mark.parametrize(
    "content",
    [
        b"",
        b"hello, this is text",
        b"<html><script>alert(1)</script></html>",
        b'<svg xmlns="http://www.w3.org/2000/svg"></svg>',
        b"GIF89a" + b"x" * 100,
        b"RIFF\x00\x00\x00\x00WAVEfmt ",
        b"\xff\xd8",  # a JPEG cut off after two bytes
    ],
)
async def test_non_images_are_refused_and_leave_no_file(
    client: AsyncClient, db_session: AsyncSession, media_root: Path, content: bytes
) -> None:
    user = await make_user(db_session)
    person = await create_person(client, user)

    response = await add_face(client, user, person["id"], content)

    assert response.status_code == 415
    assert stored_files(media_root) == []


async def test_oversized_photo_is_refused(
    client: AsyncClient, db_session: AsyncSession, media_root: Path
) -> None:
    user = await make_user(db_session)
    person = await create_person(client, user)
    too_big = JPEG + b"\x00" * (service.MAX_PHOTO_BYTES - len(JPEG) + 1)

    response = await add_face(client, user, person["id"], too_big)

    assert response.status_code == 413
    assert stored_files(media_root) == []


async def test_photo_and_people_limits(
    client: AsyncClient, db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(service, "MAX_FACES_PER_PERSON", 1)
    monkeypatch.setattr(service, "MAX_PEOPLE_PER_OWNER", 1)
    user = await make_user(db_session)
    person = await create_person(client, user)
    await face(client, user, person["id"])

    second_photo = await add_face(client, user, person["id"], JPEG + b"2")
    second_person = await client.post("/gallery/people", json=PERSON, headers=user)

    assert second_photo.status_code == 409
    assert second_person.status_code == 409


async def test_upload_to_unknown_person(
    client: AsyncClient, db_session: AsyncSession, media_root: Path
) -> None:
    user = await make_user(db_session)

    response = await add_face(client, user, str(uuid.uuid4()))

    assert response.status_code == 404
    assert stored_files(media_root) == []


# ---------- forged links ----------


async def test_tampered_and_cross_purpose_links_are_refused(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    user = await make_user(db_session)
    person = await create_person(client, user)
    photo = await face(client, user, person["id"])
    created = await client.post(f"/gallery/faces/{photo['id']}/link", headers=user)
    query = parse_qs(urlsplit(created.json()["url"]).query)
    exp, sig = int(query["exp"][0]), query["sig"][0]
    face_id = uuid.UUID(photo["id"])

    for bad in (
        f"/gallery/files/{photo['id']}?exp={exp}&sig={'0' * 64}",
        f"/gallery/files/{photo['id']}?exp={exp + 60}&sig={sig}",
        # A genuine media link must not open a gallery photo.
        f"/gallery/files/{photo['id']}?exp={exp}&sig={signed_links.sign('media', face_id, exp)}",
        f"/media/files/{photo['id']}?exp={exp}&sig={sig}",
    ):
        assert (await client.get(bad)).status_code in (403, 404), bad
    assert (await client.get(created.json()["url"])).status_code == 200


# ---------- hostile input ----------


@pytest.mark.parametrize(
    "body",
    [
        {"name": "", "category": "family", "consent_confirmed": True},
        {"name": "x" * 101, "category": "family", "consent_confirmed": True},
        {"name": "bad\x00name", "category": "family", "consent_confirmed": True},
        {"name": "two\nlines", "category": "family", "consent_confirmed": True},
        {"name": "A", "category": "vip", "consent_confirmed": True},
        {"name": "A", "category": "family", "consent_confirmed": "yes"},
        {**PERSON, "notes": "x" * 501},
        {**PERSON, "notes": "nul\x00byte"},
    ],
)
async def test_bad_person_input_is_rejected(
    client: AsyncClient, db_session: AsyncSession, body: dict
) -> None:
    user = await make_user(db_session)

    assert (
        await client.post("/gallery/people", json=body, headers=user)
    ).status_code == 422


async def test_multiline_notes_and_unicode_names_are_fine(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    user = await make_user(db_session)

    person = await create_person(
        client, user, name="अनन्या Ünal", notes="Line 1\nLine 2"
    )

    assert person["name"] == "अनन्या Ünal"
    assert person["notes"] == "Line 1\nLine 2"


@pytest.mark.parametrize(
    "result",
    [
        {"vector": [0.1] * 63},
        {"vector": [0.1] * 2049},
        {"vector": ["a"] * 128},
        {"vector": VECTOR, "rejected_reason": "both"},
        {},
        {"rejected_reason": ""},
        {"rejected_reason": "bad\x00reason"},
    ],
)
async def test_bad_embedding_results_are_rejected(
    client: AsyncClient, db_session: AsyncSession, result: dict
) -> None:
    _, _, token = await owner_with_agent(client, db_session)

    response = await report(client, token, {"face_id": str(uuid.uuid4()), **result})

    assert response.status_code == 422


@pytest.mark.parametrize("literal", ["NaN", "Infinity", "-Infinity", "1e999"])
async def test_non_finite_vectors_are_rejected(
    client: AsyncClient, db_session: AsyncSession, literal: str
) -> None:
    """Python's JSON parser accepts NaN and Infinity, so send them raw."""
    _, _, token = await owner_with_agent(client, db_session)
    vector = ",".join([literal] * 128)
    body = (
        f'{{"model": "{MODEL}", "results": '
        f'[{{"face_id": "{uuid.uuid4()}", "vector": [{vector}]}}]}}'
    )

    response = await client.post(
        "/agent/gallery/embeddings",
        content=body,
        headers={**bearer(token), "Content-Type": "application/json"},
    )

    assert response.status_code == 422


async def test_bad_embedding_reports_are_rejected(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    _, _, token = await owner_with_agent(client, db_session)
    result = {"face_id": str(uuid.uuid4()), "vector": VECTOR}

    assert (await report(client, token)).status_code == 422
    assert (await report(client, token, *[result] * 51)).status_code == 422
    for model in ("", "../etc", "has space", "x" * 65):
        assert (await report(client, token, result, model=model)).status_code == 422
        response = await client.get(
            "/agent/gallery", params={"model": model}, headers=bearer(token)
        )
        assert response.status_code == 422, model


async def test_vector_length_must_stay_the_same_per_model(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    user, _, token = await owner_with_agent(client, db_session)
    person = await create_person(client, user)
    first = await face(client, user, person["id"])
    second = await face(client, user, person["id"], JPEG + b"2")
    await report(client, token, {"face_id": first["id"], "vector": VECTOR})

    longer = await report(
        client, token, {"face_id": second["id"], "vector": [0.1] * 256}
    )
    mixed = await report(
        client,
        token,
        {"face_id": second["id"], "vector": [0.1] * 512},
        {"face_id": first["id"], "vector": [0.1] * 256},
        model="facenet-512",
    )
    other_model = await report(
        client,
        token,
        {"face_id": second["id"], "vector": [0.1] * 512},
        model="facenet-512",
    )

    assert longer.status_code == 422
    assert mixed.status_code == 422
    assert other_model.json()["stored"] == 1
