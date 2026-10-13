"""Tests for the events module: agents reporting events, attaching media,
severity, alert throttling, the user's timeline and review, and retention."""

import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from httpx import AsyncClient
from sqlalchemy import update
from sqlalchemy.ext.asyncio import AsyncSession

from api.core import storage
from api.modules.events import service
from api.modules.events.models import Event
from tests.test_audit import latest
from tests.test_cameras import add_camera
from tests.test_edge_agent import Headers, bearer
from tests.test_gallery import create_person
from tests.test_media_access import CLIP, camera_setup, upload
from tests.test_notifications import inbox


@pytest.fixture(autouse=True)
def media_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Keep every test's files in its own temporary folder."""
    monkeypatch.setattr(storage, "root", tmp_path)
    return tmp_path


# ---------- helpers ----------


def ago(**delta: float) -> str:
    return (datetime.now(UTC) - timedelta(**delta)).isoformat()


def event(camera_id: str, type: str = "person_detected", **changes) -> dict:
    return {
        "client_event_id": changes.pop("client_event_id", uuid.uuid4().hex),
        "camera_id": camera_id,
        "type": type,
        "occurred_at": changes.pop("occurred_at", ago(seconds=1)),
        **changes,
    }


async def report(client: AsyncClient, token: str, *events: dict):
    return await client.post(
        "/agent/events", json={"events": list(events)}, headers=bearer(token)
    )


async def report_ok(client: AsyncClient, token: str, *events: dict) -> list[dict]:
    response = await report(client, token, *events)
    assert response.status_code == 200, response.text
    return response.json()["results"]


async def created_id(client: AsyncClient, token: str, body: dict) -> str:
    [result] = await report_ok(client, token, body)
    assert result["status"] == "created", result
    return result["event_id"]


async def timeline(client: AsyncClient, user: Headers, **params) -> list[dict]:
    response = await client.get("/events", params=params, headers=user)
    assert response.status_code == 200, response.text
    return response.json()


async def alerts(client: AsyncClient, user: Headers) -> list[dict]:
    return [n for n in await inbox(client, user) if n["category"] == "detection"]


# ---------- reporting ----------


async def test_report_creates_event_with_snapshot_and_alerts(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    user, token, camera_id = await camera_setup(client, db_session)
    snapshot_id = await upload(client, token, camera_id)

    [result] = await report_ok(
        client,
        token,
        event(
            camera_id,
            client_event_id="evt-1",
            snapshot_media_id=snapshot_id,
            confidence=0.91,
            details={"bbox": [0.1, 0.2, 0.3, 0.4], "track_id": "t-7"},
        ),
    )

    assert result["status"] == "created"
    assert result["client_event_id"] == "evt-1"
    [shown] = await timeline(client, user)
    assert shown["id"] == result["event_id"]
    assert shown["camera_id"] == camera_id
    assert shown["type"] == "person_detected"
    assert shown["severity"] == "warning"
    assert shown["snapshot_media_id"] == snapshot_id
    assert shown["clip_media_id"] is None
    assert shown["confidence"] == 0.91
    assert shown["details"] == {"bbox": [0.1, 0.2, 0.3, 0.4], "track_id": "t-7"}
    assert shown["person"] is None
    assert shown["reviewed"] is False

    [alert] = await alerts(client, user)
    assert alert["kind"] == "person_detected"
    assert alert["title"] == "Person detected"
    assert "Front Gate" in alert["body"]
    assert alert["data"]["event_id"] == result["event_id"]
    assert alert["data"]["camera_id"] == camera_id


async def test_snapshot_may_still_be_uploading(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    """The agent reports right after registering the file, before the bytes."""
    user, token, camera_id = await camera_setup(client, db_session)
    registered = await client.post(
        "/agent/media",
        json={
            "camera_id": camera_id,
            "kind": "snapshot",
            "content_type": "image/jpeg",
            "size_bytes": 10,
            "sha256": "ab" * 32,
            "captured_at": ago(seconds=1),
        },
        headers=bearer(token),
    )
    media_id = registered.json()["media_id"]

    event_id = await created_id(
        client, token, event(camera_id, snapshot_media_id=media_id)
    )

    shown = (await client.get(f"/events/{event_id}", headers=user)).json()
    assert shown["snapshot_media_id"] == media_id


async def test_retried_batch_returns_duplicates(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    user, token, camera_id = await camera_setup(client, db_session)
    batch = [event(camera_id, client_event_id=f"evt-{i}") for i in range(3)]

    first = await report_ok(client, token, *batch)
    second = await report_ok(client, token, *batch)

    assert [r["status"] for r in first] == ["created"] * 3
    assert [r["status"] for r in second] == ["duplicate"] * 3
    assert [r["event_id"] for r in second] == [r["event_id"] for r in first]
    assert len(await timeline(client, user)) == 3


async def test_repeat_within_one_batch_is_a_duplicate(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    _, token, camera_id = await camera_setup(client, db_session)
    body = event(camera_id, client_event_id="same")

    results = await report_ok(client, token, body, body)

    assert [r["status"] for r in results] == ["created", "duplicate"]
    assert results[0]["event_id"] == results[1]["event_id"]


async def test_one_bad_event_does_not_fail_the_batch(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    user, token, camera_id = await camera_setup(client, db_session)

    results = await report_ok(
        client,
        token,
        event(camera_id),
        event(str(uuid.uuid4())),
        event(camera_id, snapshot_media_id=str(uuid.uuid4())),
        event(camera_id, occurred_at=ago(days=400)),
    )

    assert [(r["status"], r["reason"]) for r in results] == [
        ("created", None),
        ("rejected", "camera_not_found"),
        ("rejected", "media_not_found"),
        ("rejected", "too_old"),
    ]
    assert all(r["event_id"] is None for r in results[1:])
    assert len(await timeline(client, user)) == 1


async def test_clip_is_attached_later(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    user, token, camera_id = await camera_setup(client, db_session)
    event_id = await created_id(client, token, event(camera_id))
    clip_id = await upload(
        client, token, camera_id, CLIP, kind="clip", content_type="video/mp4"
    )

    attached = await client.patch(
        f"/agent/events/{event_id}",
        json={"clip_media_id": clip_id},
        headers=bearer(token),
    )
    again = await client.patch(
        f"/agent/events/{event_id}",
        json={"clip_media_id": clip_id},
        headers=bearer(token),
    )
    other_clip = await upload(
        client, token, camera_id, CLIP[::-1], kind="clip", content_type="video/mp4"
    )
    replaced = await client.patch(
        f"/agent/events/{event_id}",
        json={"clip_media_id": other_clip},
        headers=bearer(token),
    )

    assert attached.status_code == 200, attached.text
    assert attached.json() == {
        "event_id": event_id,
        "snapshot_media_id": None,
        "clip_media_id": clip_id,
    }
    assert again.status_code == 200
    assert replaced.status_code == 409
    shown = (await client.get(f"/events/{event_id}", headers=user)).json()
    assert shown["clip_media_id"] == clip_id


async def test_attach_checks_the_kind_of_file(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    _, token, camera_id = await camera_setup(client, db_session)
    event_id = await created_id(client, token, event(camera_id))
    snapshot_id = await upload(client, token, camera_id)

    as_clip = await client.patch(
        f"/agent/events/{event_id}",
        json={"clip_media_id": snapshot_id},
        headers=bearer(token),
    )
    empty = await client.patch(
        f"/agent/events/{event_id}", json={}, headers=bearer(token)
    )

    assert as_clip.status_code == 404
    assert empty.status_code == 422


# ---------- severity and people ----------


async def test_severity_follows_type_and_person_category(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    user, token, camera_id = await camera_setup(client, db_session)
    family = await create_person(client, user, name="Asha", category="family")
    blocked = await create_person(client, user, name="Mallory", category="blocked")

    await report_ok(
        client,
        token,
        event(camera_id, "motion", client_event_id="m"),
        event(camera_id, "face_unknown", client_event_id="u", face_model="arcface"),
        event(camera_id, "camera_tampered", client_event_id="t"),
        event(
            camera_id,
            "face_recognized",
            client_event_id="f",
            person_id=family["id"],
            face_model="arcface",
            confidence=0.8,
        ),
        event(
            camera_id,
            "face_recognized",
            client_event_id="b",
            person_id=blocked["id"],
        ),
    )

    by_type = {
        (e["type"], e["person"]["name"] if e["person"] else None): e
        for e in await timeline(client, user)
    }
    assert by_type[("motion", None)]["severity"] == "info"
    assert by_type[("face_unknown", None)]["severity"] == "warning"
    assert by_type[("camera_tampered", None)]["severity"] == "warning"
    assert by_type[("face_recognized", "Asha")]["severity"] == "info"
    assert by_type[("face_recognized", "Asha")]["face_model"] == "arcface"
    assert by_type[("face_recognized", "Mallory")]["severity"] == "critical"
    assert by_type[("face_recognized", "Mallory")]["person"]["category"] == "blocked"

    kinds = {a["kind"]: a for a in await alerts(client, user)}
    assert set(kinds) == {
        "motion",
        "face_unknown",
        "camera_tampered",
        "person_recognized",
        "blocked_person_seen",
    }
    assert kinds["blocked_person_seen"]["title"] == "Blocked person seen"
    assert kinds["blocked_person_seen"]["data"]["severity"] == "critical"
    assert kinds["person_recognized"]["title"] == "Asha seen"


async def test_event_survives_person_being_deleted(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    user, token, camera_id = await camera_setup(client, db_session)
    person = await create_person(client, user)
    event_id = await created_id(
        client, token, event(camera_id, "face_recognized", person_id=person["id"])
    )

    await client.delete(f"/gallery/people/{person['id']}", headers=user)

    shown = (await client.get(f"/events/{event_id}", headers=user)).json()
    assert shown["type"] == "face_recognized"
    assert shown["person"] is None


async def test_person_removed_before_report_is_stored_without_person(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    """The agent's gallery may lag behind a deletion."""
    user, token, camera_id = await camera_setup(client, db_session)

    event_id = await created_id(
        client,
        token,
        event(camera_id, "face_recognized", person_id=str(uuid.uuid4())),
    )

    shown = (await client.get(f"/events/{event_id}", headers=user)).json()
    assert shown["person"] is None
    assert shown["severity"] == "info"
    [alert] = await alerts(client, user)
    assert alert["title"] == "Someone from your gallery seen"


# ---------- alert throttling ----------


async def test_repeats_within_cooldown_alert_once(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    user, token, camera_id = await camera_setup(client, db_session)

    await report_ok(client, token, event(camera_id, occurred_at=ago(seconds=50)))
    await report_ok(
        client,
        token,
        event(camera_id, occurred_at=ago(seconds=30)),
        event(camera_id, occurred_at=ago(seconds=20)),
    )
    # A different type on the same camera is a different thing.
    await report_ok(client, token, event(camera_id, "motion"))

    assert len(await timeline(client, user)) == 4
    assert [a["kind"] for a in await alerts(client, user)] == [
        "motion",
        "person_detected",
    ]


async def test_alert_again_after_cooldown(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    user, token, camera_id = await camera_setup(client, db_session)

    await report_ok(
        client,
        token,
        event(camera_id, occurred_at=ago(seconds=150)),
        event(camera_id, occurred_at=ago(seconds=120)),
        event(camera_id, occurred_at=ago(seconds=80)),
    )
    await report_ok(client, token, event(camera_id, occurred_at=ago(seconds=10)))

    # 150s ago alerts, 120s is inside its cooldown, 80s is past it, 10s too.
    assert len(await alerts(client, user)) == 3


async def test_cooldown_is_per_camera_and_per_person(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    user, token, camera_id = await camera_setup(client, db_session)
    agent_id = (await client.get("/agents", headers=user)).json()[0]["id"]
    second_camera = (await add_camera(client, user, agent_id, name="Back Door"))["id"]
    asha = await create_person(client, user, name="Asha")
    ravi = await create_person(client, user, name="Ravi")

    await report_ok(
        client,
        token,
        event(camera_id),
        event(second_camera),
        event(camera_id, "face_recognized", person_id=asha["id"]),
        event(camera_id, "face_recognized", person_id=ravi["id"]),
        event(camera_id, "face_recognized", person_id=ravi["id"]),
    )

    titles = sorted(a["title"] for a in await alerts(client, user))
    assert titles == ["Asha seen", "Person detected", "Person detected", "Ravi seen"]


async def test_backlog_is_stored_without_alerting(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    user, token, camera_id = await camera_setup(client, db_session)

    await report_ok(client, token, event(camera_id, occurred_at=ago(hours=2)))

    assert len(await timeline(client, user)) == 1
    assert await alerts(client, user) == []


async def test_alerts_follow_detection_settings(
    client: AsyncClient, db_session: AsyncSession, pushbox: list
) -> None:
    user, token, camera_id = await camera_setup(client, db_session)
    await client.patch(
        "/notifications/preferences/detection", json={"push": False}, headers=user
    )

    await report_ok(client, token, event(camera_id))

    assert len(await alerts(client, user)) == 1  # the inbox always fills
    assert pushbox == []


# ---------- timeline ----------


async def test_timeline_filters_and_pages(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    user, token, camera_id = await camera_setup(client, db_session)
    agent_id = (await client.get("/agents", headers=user)).json()[0]["id"]
    other_camera = (await add_camera(client, user, agent_id, name="Back"))["id"]
    person = await create_person(client, user, category="blocked")
    await report_ok(
        client,
        token,
        event(camera_id, "motion", occurred_at=ago(minutes=5)),
        event(camera_id, occurred_at=ago(minutes=4)),
        event(other_camera, "motion", occurred_at=ago(minutes=3)),
        event(
            camera_id,
            "face_recognized",
            person_id=person["id"],
            occurred_at=ago(minutes=2),
        ),
    )

    everything = await timeline(client, user)
    assert [e["type"] for e in everything] == [
        "face_recognized",
        "motion",
        "person_detected",
        "motion",
    ]
    assert len(await timeline(client, user, camera_id=other_camera)) == 1
    assert len(await timeline(client, user, type="motion")) == 2
    assert len(await timeline(client, user, severity="critical")) == 1
    assert len(await timeline(client, user, person_id=person["id"])) == 1

    page_one = await timeline(client, user, limit=2)
    page_two = await timeline(client, user, limit=2, before=page_one[-1]["occurred_at"])
    assert [e["id"] for e in page_one + page_two] == [e["id"] for e in everything]


async def test_review_one_and_all(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    user, token, camera_id = await camera_setup(client, db_session)
    ids = [
        r["event_id"]
        for r in await report_ok(client, token, *[event(camera_id) for _ in range(3)])
    ]

    async def unreviewed() -> int:
        response = await client.get("/events/unreviewed-count", headers=user)
        return response.json()["unreviewed"]

    assert await unreviewed() == 3
    reviewed = await client.patch(
        f"/events/{ids[0]}", json={"reviewed": True}, headers=user
    )
    assert reviewed.json()["reviewed"] is True
    assert await unreviewed() == 2
    assert len(await timeline(client, user, unreviewed_only=True)) == 2

    undone = await client.patch(
        f"/events/{ids[0]}", json={"reviewed": False}, headers=user
    )
    assert undone.json()["reviewed"] is False

    marked = await client.post("/events/review-all", headers=user)
    assert marked.json() == {"reviewed": 3}
    assert await unreviewed() == 0


async def test_review_all_for_one_camera(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    user, token, camera_id = await camera_setup(client, db_session)
    agent_id = (await client.get("/agents", headers=user)).json()[0]["id"]
    other_camera = (await add_camera(client, user, agent_id, name="Back"))["id"]
    await report_ok(client, token, event(camera_id), event(other_camera))

    marked = await client.post(
        "/events/review-all", params={"camera_id": other_camera}, headers=user
    )

    assert marked.json() == {"reviewed": 1}
    [left] = await timeline(client, user, unreviewed_only=True)
    assert left["camera_id"] == camera_id


async def test_delete_keeps_media_and_is_audited(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    user, token, camera_id = await camera_setup(client, db_session)
    snapshot_id = await upload(client, token, camera_id)
    event_id = await created_id(
        client, token, event(camera_id, snapshot_media_id=snapshot_id)
    )

    deleted = await client.delete(f"/events/{event_id}", headers=user)

    assert deleted.status_code == 204
    assert (await client.get(f"/events/{event_id}", headers=user)).status_code == 404
    assert (await client.get(f"/media/{snapshot_id}", headers=user)).status_code == 200
    entry = await latest(client, user, "event_deleted")
    assert entry["target_id"] == event_id
    assert entry["details"] == {"type": "person_detected", "camera_id": camera_id}


async def test_deleting_media_or_camera_keeps_the_event(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    user, token, camera_id = await camera_setup(client, db_session)
    snapshot_id = await upload(client, token, camera_id)
    event_id = await created_id(
        client, token, event(camera_id, snapshot_media_id=snapshot_id)
    )

    await client.delete(f"/media/{snapshot_id}", headers=user)
    await client.delete(f"/cameras/{camera_id}", headers=user)

    shown = (await client.get(f"/events/{event_id}", headers=user)).json()
    assert shown["snapshot_media_id"] is None
    assert shown["camera_id"] is None


# ---------- retention ----------


async def test_purge_removes_only_expired_events(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    user, token, camera_id = await camera_setup(client, db_session)
    old_id, new_id = [
        r["event_id"]
        for r in await report_ok(client, token, event(camera_id), event(camera_id))
    ]
    await db_session.execute(
        update(Event)
        .where(Event.id == uuid.UUID(old_id))
        .values(occurred_at=datetime.now(UTC) - timedelta(days=400))
    )
    await db_session.commit()

    removed = await service.purge_expired(db_session)

    assert removed == 1
    assert [e["id"] for e in await timeline(client, user)] == [new_id]
