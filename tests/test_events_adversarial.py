"""Hostile and edge-case tests for the events module: agents and people
reaching for each other's cameras, files, people and events, and malformed
reports."""

import json
import uuid
from pathlib import Path

import pytest
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from api.core import storage
from api.modules.events import repository
from tests.test_cameras import add_camera
from tests.test_edge_agent import bearer
from tests.test_events import ago, created_id, event, report, report_ok, timeline
from tests.test_gallery import create_person
from tests.test_media_access import CLIP, camera_setup, upload


@pytest.fixture(autouse=True)
def media_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setattr(storage, "root", tmp_path)
    return tmp_path


async def two_owners(client: AsyncClient, db_session: AsyncSession):
    """(user, token, camera_id) for an owner and for a stranger."""
    owner = await camera_setup(client, db_session, "owner@example.com")
    stranger = await camera_setup(client, db_session, "stranger@example.com")
    return owner, stranger


# ---------- other people's things ----------


async def test_agent_cannot_report_on_someone_elses_camera(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    (owner, _, camera_id), (stranger, stranger_token, _) = await two_owners(
        client, db_session
    )

    [result] = await report_ok(client, stranger_token, event(camera_id))

    assert result == {
        "client_event_id": result["client_event_id"],
        "status": "rejected",
        "event_id": None,
        "reason": "camera_not_found",
    }
    assert await timeline(client, owner) == []
    assert await timeline(client, stranger) == []


async def test_agent_cannot_attach_someone_elses_media(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    (_, token, camera_id), (_, stranger_token, stranger_camera) = await two_owners(
        client, db_session
    )
    their_snapshot = await upload(client, stranger_token, stranger_camera)

    [result] = await report_ok(
        client, token, event(camera_id, snapshot_media_id=their_snapshot)
    )

    assert result["reason"] == "media_not_found"


async def test_media_must_come_from_the_events_camera_and_slot(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    user, token, camera_id = await camera_setup(client, db_session)
    agent_id = (await client.get("/agents", headers=user)).json()[0]["id"]
    other_camera = (await add_camera(client, user, agent_id, name="Back"))["id"]
    other_snapshot = await upload(client, token, other_camera)
    snapshot = await upload(client, token, camera_id)
    clip = await upload(
        client, token, camera_id, CLIP, kind="clip", content_type="video/mp4"
    )

    results = await report_ok(
        client,
        token,
        event(camera_id, snapshot_media_id=other_snapshot),
        event(camera_id, snapshot_media_id=clip),
        event(camera_id, clip_media_id=snapshot),
        event(camera_id, snapshot_media_id=snapshot, clip_media_id=clip),
    )

    assert [r["reason"] for r in results] == [
        "media_not_found",
        "media_not_found",
        "media_not_found",
        None,
    ]


async def test_someone_elses_person_is_not_linked_or_revealed(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    (owner, token, camera_id), (stranger, _, _) = await two_owners(client, db_session)
    their_blocked = await create_person(
        client, stranger, name="Secret", category="blocked"
    )

    event_id = await created_id(
        client,
        token,
        event(camera_id, "face_recognized", person_id=their_blocked["id"]),
    )

    shown = (await client.get(f"/events/{event_id}", headers=owner)).json()
    assert shown["person"] is None
    assert shown["severity"] == "info"
    assert "Secret" not in json.dumps(shown)


async def test_people_cannot_see_or_change_each_others_events(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    (owner, token, camera_id), (stranger, _, _) = await two_owners(client, db_session)
    event_id = await created_id(client, token, event(camera_id))

    assert (
        await client.get(f"/events/{event_id}", headers=stranger)
    ).status_code == 404
    patched = await client.patch(
        f"/events/{event_id}", json={"reviewed": True}, headers=stranger
    )
    deleted = await client.delete(f"/events/{event_id}", headers=stranger)
    reviewed_all = await client.post("/events/review-all", headers=stranger)
    count = await client.get("/events/unreviewed-count", headers=stranger)

    assert patched.status_code == 404
    assert deleted.status_code == 404
    assert reviewed_all.json() == {"reviewed": 0}
    assert count.json() == {"unreviewed": 0}
    [shown] = await timeline(client, owner)
    assert shown["reviewed"] is False


async def test_agent_cannot_patch_another_agents_event(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    (_, token, camera_id), (_, stranger_token, stranger_camera) = await two_owners(
        client, db_session
    )
    event_id = await created_id(client, token, event(camera_id))
    their_clip = await upload(
        client,
        stranger_token,
        stranger_camera,
        CLIP,
        kind="clip",
        content_type="video/mp4",
    )

    response = await client.patch(
        f"/agent/events/{event_id}",
        json={"clip_media_id": their_clip},
        headers=bearer(stranger_token),
    )

    assert response.status_code == 404


async def test_same_client_id_from_two_agents_is_two_events(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    (_, token, camera_id), (_, stranger_token, stranger_camera) = await two_owners(
        client, db_session
    )

    [mine] = await report_ok(client, token, event(camera_id, client_event_id="1"))
    [theirs] = await report_ok(
        client, stranger_token, event(stranger_camera, client_event_id="1")
    )

    assert mine["status"] == theirs["status"] == "created"
    assert mine["event_id"] != theirs["event_id"]


# ---------- authentication ----------


async def test_endpoints_need_the_right_credentials(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    user, token, camera_id = await camera_setup(client, db_session)

    assert (await client.get("/events")).status_code == 401
    assert (await client.post("/agent/events", json={"events": []})).status_code == 401
    # A user's login token is not an agent token, and the other way round.
    as_user = await client.post(
        "/agent/events", json={"events": [event(camera_id)]}, headers=user
    )
    as_agent = await client.get("/events", headers=bearer(token))
    assert as_user.status_code == 401
    assert as_agent.status_code == 401


async def test_revoked_agent_cannot_report(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    user, token, camera_id = await camera_setup(client, db_session)
    agent_id = (await client.get("/agents", headers=user)).json()[0]["id"]
    await client.post(f"/agents/{agent_id}/revoke", headers=user)

    response = await report(client, token, event(camera_id))

    assert response.status_code == 401
    assert await timeline(client, user) == []


# ---------- malformed reports ----------


@pytest.mark.parametrize(
    "changes",
    [
        {"type": "explosion"},
        {"type": "motion", "person_id": str(uuid.uuid4())},
        {"type": "face_recognized"},
        {"type": "motion", "face_model": "arcface"},
        {"face_model": "has spaces"},
        {"occurred_at": ago(minutes=-30)},
        {"occurred_at": "2026-10-04T10:00:00"},  # no time zone
        {"client_event_id": ""},
        {"client_event_id": "x" * 65},
        {"client_event_id": "bad\x00id"},
        {"client_event_id": "new\nline"},
        {"confidence": 1.5},
        {"confidence": -0.1},
        {"details": {"bbox": [0.1, 0.2, 0.3]}},
        {"details": {"bbox": [0.1, 0.2, 0.3, 2]}},
        {"details": {"note": "free text is not allowed"}},
        {"details": {"track_id": "t\x00"}},
    ],
)
async def test_malformed_events_are_refused(
    client: AsyncClient, db_session: AsyncSession, changes: dict
) -> None:
    user, token, camera_id = await camera_setup(client, db_session)

    response = await report(client, token, {**event(camera_id), **changes})

    assert response.status_code == 422, response.text
    assert await timeline(client, user) == []


async def test_nan_and_infinity_are_refused(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    _, token, camera_id = await camera_setup(client, db_session)
    for value in ("NaN", "Infinity"):
        body = json.dumps({"events": [event(camera_id)]})[:-2]
        body += f', "confidence": {value}}}]}}'

        response = await client.post(
            "/agent/events",
            content=body,
            headers={**bearer(token), "Content-Type": "application/json"},
        )

        assert response.status_code == 422, response.text


async def test_batch_size_is_limited(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    _, token, camera_id = await camera_setup(client, db_session)

    empty = await report(client, token)
    too_many = await report(client, token, *[event(camera_id) for _ in range(101)])
    full = await report(client, token, *[event(camera_id) for _ in range(100)])

    assert empty.status_code == 422
    assert too_many.status_code == 422
    assert full.status_code == 200


async def test_bad_filters_are_refused(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    user, _, _ = await camera_setup(client, db_session)

    for params in (
        {"type": "explosion"},
        {"severity": "apocalyptic"},
        {"limit": 0},
        {"limit": 101},
        {"camera_id": "not-a-uuid"},
    ):
        response = await client.get("/events", params=params, headers=user)
        assert response.status_code == 422, params


async def test_racing_retry_reports_duplicates(
    client: AsyncClient, db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Two copies of a batch arrive at once: the loser's insert hits the unique
    constraint, and it answers with the winner's events instead of failing."""
    user, token, camera_id = await camera_setup(client, db_session)
    body = event(camera_id, client_event_id="raced")
    [first] = await report_ok(client, token, body)

    real = repository.existing_client_ids
    calls = 0

    async def blind_once(*args, **kwargs):
        # The first lookup misses, as if the winner had not committed yet.
        nonlocal calls
        calls += 1
        return {} if calls == 1 else await real(*args, **kwargs)

    monkeypatch.setattr(repository, "existing_client_ids", blind_once)
    [second] = await report_ok(client, token, body)

    assert calls == 2
    assert second == {**first, "status": "duplicate"}
    assert len(await timeline(client, user)) == 1
